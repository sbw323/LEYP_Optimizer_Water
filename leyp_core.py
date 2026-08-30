import math

import numpy as np

from leyp_config import (
    ALPHA,
    BREAK_CONDITION_PENALTY,
    BREAK_DAMAGE_CONDITION_FLOOR,
    COEFF_DIAMETER,
    DEGRADATION_PARAMS,
    EMERGENCY_REPAIR_COST_PER_BREAK,
    HAZARD_LENGTH_SCALE,
    LEYP_BREAK_FEEDBACK_CAP,
    MATERIAL_PROPS,
    N_SEGMENTS_PER_PIPE,
    SEGMENT_BREAK_THRESHOLD,
    STANDARD_LIFE,
    map_condition_to_n_start,
)


class VirtualSegment:
    def __init__(self, segment_length):
        self.length = segment_length
        self.n_point_breaks = 0

    def simulate_breaks(self, intensity):
        """Simulate point breaks using Poisson process.

        Args:
            intensity: Hazard intensity (events per unit length per unit time)

        Returns:
            int: Number of point breaks that occurred
        """
        num_events = np.random.poisson(intensity * self.length / HAZARD_LENGTH_SCALE)
        self.n_point_breaks += num_events
        return num_events

    def has_failed(self, threshold):
        """Check if segment has failed based on break count threshold.

        Args:
            threshold: Break count threshold for failure

        Returns:
            bool: True if n_point_breaks >= threshold
        """
        return self.n_point_breaks >= threshold

    def reset(self):
        """Reset segment break history."""
        self.n_point_breaks = 0


class Pipe:
    def __init__(self, attributes):
        """Initialize pipe with condition from age + CSV assessment, and break seeding.

        Condition uses exponential decay on age/base_life ratio so that pipes
        older than their design life start at low-but-nonzero condition
        instead of being clamped to 1.0 (which made them invisible to the
        simulation).  The CSV Condition column acts as a modifier: newer-era
        pipes (Condition >= 2) receive a condition boost.

        Args:
            attributes (dict): Pipe attributes including PipeID, Material, Diameter,
                Length, CoF_Value, Age, Condition
        """
        self.id = attributes["PipeID"]
        self.material = attributes["Material"]
        self.diameter = attributes["Diameter"]
        self.length = attributes["Length"]
        self.cof = float(attributes.get("CoF_Value", 1.0))

        self.initial_age = attributes["Age"]

        # --- Condition initialization (B2/B4 fix) ---
        material_life = STANDARD_LIFE.get(self.material, STANDARD_LIFE["Default"])
        base_life = material_life["base_life"]

        # life_fraction can exceed 1.0 for pipes older than design life
        life_fraction = self.initial_age / base_life

        # Exponential decay: approaches 1.0 asymptotically but never reaches it.
        # At lf=0 → 6.0, lf=1.0 → ~2.12, lf=1.5 → ~1.53, lf=2.0 → ~1.25
        age_condition = 1.0 + 5.0 * math.exp(-1.5 * life_fraction)

        # CSV condition modifier (B2 fix: use the field assessment data).
        # In this dataset the column is a binary era indicator:
        # 1 = older infrastructure (age 46+), 2 = newer additions (age 26).
        # Condition >= 2 blends the estimate upward toward 6.0.
        csv_rating = attributes.get("Condition", None)
        if csv_rating is not None:
            boost = max(0.0, (float(csv_rating) - 1) * 0.5)
        else:
            boost = 0.0

        self.current_condition = min(6.0, age_condition + boost * (6.0 - age_condition))

        # Virtual Segments
        seg_len = self.length / N_SEGMENTS_PER_PIPE
        self.segments = [VirtualSegment(seg_len) for _ in range(N_SEGMENTS_PER_PIPE)]

        # Seed breaks based on age (use clamped fraction for seeding)
        self._seed_breaks(min(1.0, life_fraction))

        self.reset_physics_params()
        self.update_leyp_state()
        self.has_failed_in_sim = False

    def _seed_breaks(self, life_fraction):
        """Seed historical breaks based on age, uniformly across segments.

        Seeded breaks represent repairs the utility has already made.  Their
        purpose is the LEYP feedback term (1 + alpha * n_breaks): a pipe that
        has broken before is more likely to break again.  They must not, on
        their own, condemn a pipe — the inventory lists these pipes as in
        service, so a pipe that starts the simulation already past the failure
        rule contradicts its own input data (review finding A1).

        Each segment is therefore capped one break below
        SEGMENT_BREAK_THRESHOLD.  The failure rule itself is unchanged: any
        single segment reaching the threshold still condemns the pipe, and
        seeded breaks still count toward it — a segment seeded at the cap
        fails on its next break.

        Args:
            life_fraction (float): Fraction of standard life already lived (0-1)
        """
        max_expected = int(life_fraction * 6)
        if max_expected <= 0:
            return

        n_seeded = np.random.randint(0, max_expected + 1)
        seed_cap = SEGMENT_BREAK_THRESHOLD - 1

        for _ in range(n_seeded):
            available = [s for s in self.segments if s.n_point_breaks < seed_cap]
            if not available:
                break  # Every segment is at the cap; discard the remainder.
            available[np.random.randint(0, len(available))].n_point_breaks += 1

    def reset_physics_params(self):
        mat_params = MATERIAL_PROPS.get(self.material, MATERIAL_PROPS["Default"])
        self.beta = mat_params["beta"]
        self.eta = mat_params["eta"]
        self.mat_mult = mat_params["base_mult"]

        deg_params = DEGRADATION_PARAMS.get(self.material, DEGRADATION_PARAMS["Default"])
        mu_years = deg_params["ttf_mean"]
        sigma_years = deg_params["ttf_std"]

        phi = math.sqrt(sigma_years**2 + mu_years**2)
        log_sigma = math.sqrt(math.log(phi**2 / mu_years**2))
        log_mu = math.log(mu_years**2 / phi)

        self.total_ttf_years = max(10, np.random.lognormal(log_mu, log_sigma))
        self.degradation_rate = math.log(6.0) / self.total_ttf_years

    @property
    def current_ttf(self):
        safe_cond = max(1.001, self.current_condition)
        rul = math.log(safe_cond) / self.degradation_rate
        return max(0.1, rul)

    def predict_ttf(self, hypothetical_cond, material_override=None):
        if material_override and material_override != self.material:
            deg_params = DEGRADATION_PARAMS.get(material_override, DEGRADATION_PARAMS["Default"])
            return deg_params["ttf_mean"]
        else:
            safe_cond = max(1.001, hypothetical_cond)
            rul = math.log(safe_cond) / self.degradation_rate
            return max(0.1, rul)

    def update_leyp_state(self):
        """Synchronize pipe-level break count from virtual segment state.

        The LEYP feedback term (1 + α·n_breaks) requires the cumulative
        break count across all segments — the actual break history, not a
        condition-derived proxy.  Virtual segments already accumulate
        Poisson events in n_point_breaks, so summing them gives the
        correct pipe-level count at every point in the simulation.
        """
        self.n_breaks = sum(seg.n_point_breaks for seg in self.segments)
        if not hasattr(self, "initial_n_breaks"):
            self.initial_n_breaks = self.n_breaks

    def reset_breaks(self):
        """
        Resets break history for repairs.
        """
        for seg in self.segments:
            seg.reset()
        self.update_leyp_state()

    def degrade(self, dt=1.0):
        """Apply exponential degradation to pipe condition.

        Args:
            dt (float): Time step in years (default: 1.0)
        """
        decay_factor = math.exp(-self.degradation_rate * dt)
        self.current_condition *= decay_factor
        self.current_condition = max(1.0, min(6.0, self.current_condition))
        self.update_leyp_state()

    def calculate_hazard(self, sim_year_idx):
        current_age = self.initial_age + sim_year_idx
        t = max(current_age, 0.1)
        h0 = (self.beta / self.eta) * ((t / self.eta) ** (self.beta - 1))
        cov_factor = self.mat_mult * np.exp(COEFF_DIAMETER * self.diameter)
        # Saturating feedback: break history raises hazard but the effect
        # plateaus.  An unbounded term diverges once pipes stop being renewed.
        effective_breaks = min(self.n_breaks, LEYP_BREAK_FEEDBACK_CAP)
        leyp_factor = 1.0 + (ALPHA * effective_breaks)
        return h0 * cov_factor * leyp_factor

    def simulate_year(self, sim_year_idx):
        """Simulate one year of pipe operation with break generation.

        Args:
            sim_year_idx (int): Simulation year index

        Returns:
            dict: Dictionary with keys 'breaks', 'repair_cost', 'failed'
        """
        intensity = self.calculate_hazard(sim_year_idx)
        total_new_breaks = 0
        failed = False

        # Simulate breaks in each segment
        for seg in self.segments:
            n = seg.simulate_breaks(intensity)
            total_new_breaks += n

        # Apply damage from new breaks.  Floored above the failure threshold:
        # this coupling exists to make a frequently-breaking pipe read as poor
        # condition (and so become CIP-eligible), not to act as a third,
        # independent route to failure alongside age-out and the segment rule.
        if total_new_breaks > 0:
            damage = BREAK_CONDITION_PENALTY * total_new_breaks
            self.current_condition = max(
                BREAK_DAMAGE_CONDITION_FLOOR, self.current_condition - damage
            )
            self.update_leyp_state()

        # Check for segment failure (regardless of whether new breaks occurred)
        for seg in self.segments:
            if seg.has_failed(SEGMENT_BREAK_THRESHOLD):
                self.current_condition = 1.0
                failed = True
                break

        repair_cost = total_new_breaks * EMERGENCY_REPAIR_COST_PER_BREAK

        return {"breaks": total_new_breaks, "repair_cost": repair_cost, "failed": failed}