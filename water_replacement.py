"""
Water main replacement planning module.

Provides ReplacementManager for budget-constrained CIP replacement decisions.
Replaces the previous InvestmentManager with simplified water-focused logic.
"""

from typing import Any

from leyp_config import (
    ACTION_CIP_REPLACEMENT,
    CIP_REPLACEMENT_COST_PER_INCH_FT,
    DEFAULT_REPLACEMENT_MATERIAL,
    EMERGENCY_REPLACEMENT_COST_PER_FT,
    TRIGGERS,
)
from leyp_core import VirtualSegment


class ReplacementManager:
    """
    Manages water main replacement decisions using risk-based prioritization.
    
    Single-action manager focused on full pipe replacement with budget constraints.
    Prioritizes pipes by annualized risk (consequence / time-to-failure).
    """

    def __init__(
        self,
        budget: float,
        rehab_trigger: float = None,
        cip_cost_rate: float = None,
        replacement_material: str = None,
        risk_cost_per_ft: float = None,
        **kwargs  # Accept and ignore legacy parameters
    ):
        """
        Initialize replacement manager.
        
        Args:
            budget: Annual CIP budget in dollars
            rehab_trigger: Condition threshold below which pipes are eligible
            cip_cost_rate: Cost per inch-foot for CIP replacement
            replacement_material: Default material for new pipes
            risk_cost_per_ft: Emergency replacement cost for risk calculations
            **kwargs: Legacy parameters (ignored for compatibility)
        """
        self.budget = budget
        self.rehab_trigger = rehab_trigger if rehab_trigger is not None else TRIGGERS['Rehab']
        self.cip_cost_rate = cip_cost_rate if cip_cost_rate is not None else CIP_REPLACEMENT_COST_PER_INCH_FT
        self.replacement_material = replacement_material if replacement_material is not None else DEFAULT_REPLACEMENT_MATERIAL
        self.risk_cost_per_ft = risk_cost_per_ft if risk_cost_per_ft is not None else EMERGENCY_REPLACEMENT_COST_PER_FT

        self.action_log: list[dict[str, Any]] = []

    def calculate_cost(self, pipe) -> float:
        """
        Calculate replacement cost for a pipe.
        
        Args:
            pipe: Pipe object with diameter and length attributes
            
        Returns:
            Replacement cost in dollars (rate * diameter * length)
        """
        return self.cip_cost_rate * pipe.diameter * pipe.length

    def get_annualized_risk(self, pipe) -> float:
        """
        Calculate annualized risk for prioritization.
        
        Args:
            pipe: Pipe object with length, cof, and current_ttf attributes
            
        Returns:
            Annualized risk value (consequence / time_to_failure)
        """
        # Calculate consequence as length * CoF * replacement cost per foot
        consequence = pipe.length * pipe.cof * self.risk_cost_per_ft

        # Use minimum TTF of 0.1 to handle near-zero values
        safe_ttf = max(0.1, pipe.current_ttf)

        return consequence / safe_ttf

    def get_priority_score(self, pipe) -> float:
        """Rank pipes by risk reduction per dollar of CIP spend.

        Ranking on annualized risk alone (review finding B2) is
        length-dominant: risk scales with length, so the longest — and
        therefore least affordable — pipes sort to the top, where they
        consume or strand the whole annual budget.

        Dividing by replacement cost makes the ranking a benefit/cost ratio.
        Length appears in both terms and cancels, so priority is driven by
        what actually earns the money: consequence of failure, imminence of
        failure, and how cheap the pipe is to renew per foot.

        Args:
            pipe: Pipe object

        Returns:
            Annualized risk per dollar of replacement cost
        """
        cost = self.calculate_cost(pipe)
        if cost <= 0:
            return float("inf")  # Free replacement always wins the ranking.
        return self.get_annualized_risk(pipe) / cost

    def run_year(self, network: list, year: int) -> dict[str, Any]:
        """
        Execute annual replacement decisions within budget constraints.
        
        Args:
            network: List of Pipe objects
            year: Current simulation year
            
        Returns:
            Dictionary with keys 'Year', 'Spend', 'Count'
        """
        # Eligible = at or below the trigger.  Pipes at the failure floor stay
        # eligible (review finding B3): excluding them handed every age-out
        # pipe to the emergency stream, which is the work a CIP programme
        # exists to capture.
        eligible_pipes = [
            pipe for pipe in network if pipe.current_condition <= self.rehab_trigger
        ]

        # Sort by risk reduction per dollar (highest first)
        eligible_pipes.sort(key=self.get_priority_score, reverse=True)

        total_spend = 0.0
        replacement_count = 0
        unfundable_count = 0
        unfundable_length = 0.0
        deferred_count = 0

        for pipe in eligible_pipes:
            cost = self.calculate_cost(pipe)

            # A pipe costing more than a whole year's budget can never be
            # funded in one year.  Report it rather than letting it block the
            # queue; it is a multi-year programming problem, not a ranking one.
            if cost > self.budget:
                unfundable_count += 1
                unfundable_length += pipe.length
                continue

            if total_spend + cost <= self.budget:
                self.execute_replacement(pipe, year)
                total_spend += cost
                replacement_count += 1
            else:
                # Skip, do not stop: cheaper pipes further down the ranking can
                # still use the remaining budget (review finding B1).
                deferred_count += 1

        return {
            'Year': year,
            'Spend': total_spend,
            'Count': replacement_count,
            'Eligible': len(eligible_pipes),
            'Deferred': deferred_count,
            'Unfundable': unfundable_count,
            'Unfundable_Length': unfundable_length,
        }

    def execute_replacement(self, pipe, year: int) -> None:
        """
        Execute pipe replacement and log action.
        
        Args:
            pipe: Pipe object to replace
            year: Current simulation year
        """
        # Record pre-replacement state for logging.  The ranking values must be
        # captured BEFORE the reset: computing them afterwards described the
        # brand-new pipe instead of the decision that was made.
        pre_condition = pipe.current_condition
        original_material = pipe.material
        cost = self.calculate_cost(pipe)
        annualized_risk = self.get_annualized_risk(pipe)
        priority = self.get_priority_score(pipe)

        # Reset pipe to new condition
        pipe.current_condition = 6.0
        pipe.material = self.replacement_material
        pipe.initial_age = -year  # Negative age indicates new pipe
        pipe.has_failed_in_sim = False

        # Reset virtual segments
        seg_len = pipe.length / 4.0
        pipe.segments = [VirtualSegment(seg_len) for _ in range(4)]

        # Reset physics parameters for new material
        pipe.reset_physics_params()
        pipe.update_leyp_state()

        # Log the action
        self.action_log.append({
            'Year': year,
            'PipeID': pipe.id,
            'Action': ACTION_CIP_REPLACEMENT,  # Use constant from config
            'PreCondition': pre_condition,
            'PostCondition': 6.0,
            'Condition_Before': pre_condition,  # Alias for validator compatibility
            'Priority': priority,  # Risk reduction per dollar, used for ranking
            'Annualized_Risk': annualized_risk,
            'Material': original_material,  # Original material
            'NewMaterial': self.replacement_material,  # Replacement material
            'Cost': cost,
            'Length': pipe.length,
            'Diameter': pipe.diameter
        })
