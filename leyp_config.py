import pandas as pd

# ==========================================
# 1. FILE & COLUMN MAPPING
# ==========================================
REAL_DATA_PATH = "Louisa_wConduits_Input_CSV.csv"
SIMULATION_START_YEAR = 2026

COLUMN_MAP = {
    "PipeID": "PipeID",
    "Age": "Age",
    "Condition": "Condition",
    "Material": "Material",
    "Diameter": "Diameter",
    "Length": "Length",
    "CoF_Value": "CoF_Value",
}

# ==========================================
# 2. LEYP MODEL PARAMETERS (Failure Physics)
# ==========================================
ALPHA = 0.15

# Ceiling on the break count that feeds the LEYP term (1 + ALPHA * n_breaks).
# The hazard elevation from break history saturates in practice: a main with
# twenty repairs is not four times worse than one with ten.  Without a ceiling
# the term is an unbounded positive feedback loop — each break raises hazard,
# which produces more breaks — and it diverges in any run where pipes are not
# renewed.  Renewal masked this: the treated network never accumulated enough
# breaks on one pipe to run away, but an untreated baseline reached ~1e7
# breaks/mile/year.  At the default cap the feedback tops out at 2.5x.
LEYP_BREAK_FEEDBACK_CAP = 10

# --- Wear-out: the age-related arm of the bathtub curve ---
# Hazard is multiplied by a factor that is exactly 1.0 until a pipe reaches
# WEAROUT_ONSET_LIFE_RATIO times its material's STANDARD_LIFE.base_life, then
# rises as  1 + (MAX - 1) * (1 - exp(-STEEPNESS * excess_life_ratio))  and
# saturates at WEAROUT_MAX_MULTIPLIER.  With the defaults: 1.0x through average
# life, 2.6x at 110% of it, 4.5x at 125%, 6.7x at 150%, 9.8x at 300%.
#
# This is what makes "end of life" mean something in the physics.  Before it
# existed, a pipe at its condition floor broke no faster than a healthy pipe of
# the same age, and the model compensated with a rule that emergency-replaced
# any pipe the moment it reached the floor - which is what drove the optimizer
# to spend without limit.  That rule is gone; wear-out replaces it.
#
# The multiplier is bounded, so it cannot diverge; it resets on renewal, since a
# replaced pipe's age restarts at zero; and a value of 1.0 for the maximum
# disables it exactly.  Onset runs on STANDARD_LIFE.base_life by decision, not
# on DEGRADATION_PARAMS.ttf_mean (the condition clock) - the two differ by ~25%,
# so a pipe can reach its condition floor before wear-out begins.  That gap is
# reported per year as Floor_Pipes_Below_Onset.
WEAROUT_ONSET_LIFE_RATIO = 1.0
WEAROUT_MAX_MULTIPLIER = 10.0
WEAROUT_STEEPNESS = 2.0

# Condition penalty applied per break in a year (review finding C1).  Breaks
# already influence the model twice — they raise future hazard through the
# LEYP term and accumulate toward the segment failure rule — so this coupling
# exists only to make a frequently-breaking pipe read as poor condition and
# therefore become CIP-eligible.  It is floored above the failure threshold by
# BREAK_DAMAGE_CONDITION_FLOOR so it cannot condemn a pipe on its own, which
# would be a third independent failure path.
BREAK_CONDITION_PENALTY = 0.3
BREAK_DAMAGE_CONDITION_FLOOR = 1.05

# Weibull Baseline (Water Materials)
MATERIAL_PROPS = {
    "CI": {"beta": 1.8, "eta": 75, "base_mult": 1.3},
    "DI": {"beta": 1.5, "eta": 90, "base_mult": 1.0},
    "AC": {"beta": 2.0, "eta": 60, "base_mult": 1.5},
    "PVC": {"beta": 1.1, "eta": 110, "base_mult": 0.7},
    "PCCP": {"beta": 1.6, "eta": 70, "base_mult": 1.2},
    "CU": {"beta": 1.2, "eta": 85, "base_mult": 0.9},
    "HDPE": {"beta": 1.0, "eta": 120, "base_mult": 0.6},
    "Steel": {"beta": 1.4, "eta": 80, "base_mult": 1.1},
    "Default": {"beta": 1.3, "eta": 85, "base_mult": 1.0},
    "GI": {"beta": 2.2, "eta": 45, "base_mult": 1.75},
}
COEFF_DIAMETER = -0.02

# ==========================================
# 3. DEGRADATION PHYSICS
# ==========================================
DEGRADATION_PARAMS = {
    "CI": {"ttf_mean": 60, "ttf_std": 25},
    "DI": {"ttf_mean": 75, "ttf_std": 20},
    "AC": {"ttf_mean": 45, "ttf_std": 15},
    "PVC": {"ttf_mean": 90, "ttf_std": 18},
    "PCCP": {"ttf_mean": 55, "ttf_std": 20},
    "CU": {"ttf_mean": 70, "ttf_std": 22},
    "HDPE": {"ttf_mean": 100, "ttf_std": 15},
    "Steel": {"ttf_mean": 65, "ttf_std": 25},
    "Default": {"ttf_mean": 70, "ttf_std": 20},
    "GI": {"ttf_mean": 40, "ttf_std": 15},
}

# ==========================================
# 4. WATER MAIN STANDARD LIFE (Years)
# ==========================================
STANDARD_LIFE = {
    "CI": {"base_life": 75, "min_life": 50, "max_life": 100},
    "DI": {"base_life": 100, "min_life": 80, "max_life": 120},
    "AC": {"base_life": 60, "min_life": 40, "max_life": 80},
    "PVC": {"base_life": 120, "min_life": 100, "max_life": 150},
    "PCCP": {"base_life": 75, "min_life": 60, "max_life": 90},
    "CU": {"base_life": 85, "min_life": 70, "max_life": 100},
    "HDPE": {"base_life": 150, "min_life": 120, "max_life": 200},
    "Steel": {"base_life": 80, "min_life": 60, "max_life": 100},
    "Default": {"base_life": 85, "min_life": 70, "max_life": 100},
    "GI": {"base_life": 50, "min_life": 40, "max_life": 60},
}

# ==========================================
# 5. VIRTUAL SEGMENT PARAMETERS
# ==========================================
N_SEGMENTS_PER_PIPE = 4
SEGMENT_BREAK_THRESHOLD = 3

# Length normalisation for the Poisson break intensity: a segment's expected
# breaks are hazard * segment_length / HAZARD_LENGTH_SCALE.  This is a pure
# units constant with no physical claim of its own, so it is the right knob to
# calibrate the model's absolute break production against observed rates.
#
# Set by the owner at 8,600 (2026-11-11), twice the value derived on a 10-year
# untreated window (CALIBRATION_WINDOW_YEARS) of the Louisa inventory as it
# stands - no CIP, no renewal - against 0.287 breaks/mile/year (4,300,
# 2026-11-02).  Measured at 8,600: 0.128 breaks/mile/year over the window
# (8 replicates).  Anchor: Folkman (2018) national averages weighted by this
# network's material mix give 0.077 across all ages, so the network sits
# about 1.7x above it.
#
# Expected breaks scale as 1 / HAZARD_LENGTH_SCALE.  The derivation assumed
# WEAROUT_MAX_MULTIPLIER = 10; revisit if the inventory, the material
# parameters, ALPHA, or any wear-out constant changes.
HAZARD_LENGTH_SCALE = 8600.0
SIMULATION_YEARS = 100

# Horizon used when deriving HAZARD_LENGTH_SCALE.  The constant is static; this
# is the scenario it was tuned against: the inventory as it stands, run with no
# intervention for this many years, matched to the target break rate.  A short
# window measures today's network - what a published breaks/mile/year statistic
# describes.  The full 100-year do-nothing run is kept as a stability check only,
# because under wear-out most of that run is spent with the whole network far
# past average life at the saturated multiplier, a state no real utility is in.
CALIBRATION_WINDOW_YEARS = 10

# ==========================================
# 6. COST PARAMETERS
# ==========================================

# Real discount rate applied to both objectives.  Without discounting a dollar
# spent in year 100 counts the same as one spent today, which removes any
# time-value reason to sequence work and lets the optimiser treat a distant
# emergency as equivalent to an immediate one.  3% real is conventional for
# water utility capital planning; set to 0.0 to recover undiscounted totals.
DISCOUNT_RATE = 0.03

# Annual Budget
ANNUAL_BUDGET = 50000

# Water Main Replacement Costs
CIP_REPLACEMENT_COST_PER_INCH_FT = 25.00
EMERGENCY_REPAIR_COST_PER_BREAK = 5000.00

# Emergency replacement carries a premium over planned CIP work: mobilisation,
# out-of-hours crews, service interruption, road reinstatement and the loss of
# any opportunity to upsize or co-ordinate with other works.  At $5,000/ft
# against CIP's $25/inch-ft this is roughly 33x for a 6-inch main and 17x
# for a 12-inch one - well above the 2-5x range typical of utility practice
# (it was 6.9x and 3.5x at the earlier $120/inch-ft), so planned renewal is
# strongly favoured.
# This rate is also the consequence term in ReplacementManager risk scoring;
# because it enters both risk and cost uniformly it scales the objectives
# without altering the replacement ranking.
EMERGENCY_REPLACEMENT_COST_PER_FT = 5000.00
DEFAULT_REPLACEMENT_MATERIAL = "HDPE"

# ==========================================
# 7. INTERVENTION TRIGGERS
# ==========================================
TRIGGERS = {"Rehab": 2.0}

# Condition at or below which a pipe counts as inherited backlog in year 1.
# Fixed deliberately rather than tied to the run's rehab trigger: the backlog
# is a property of the inventory and must mean the same thing across every
# strategy the optimizer evaluates.
BACKLOG_CONDITION = 2.0

# ==========================================
# 8. ACTION CONSTANTS
# ==========================================
ACTION_CIP_REPLACEMENT = "CIP_Replacement"
ACTION_EMERGENCY_REPLACEMENT = "Emergency_Replacement"
ACTION_BREAK_EVENT = "Break_Event"

# ==========================================
# 9. CHECKPOINT CONFIGURATION
# ==========================================
NSGA2_CHECKPOINT_PATH = "nsga2_checkpoint.pkl"
NSGA2_CHECKPOINT_EVERY_N_GEN = 1


# ==========================================
# 10. HELPER FUNCTIONS
# ==========================================
def map_condition_to_n_start(rating):
    if pd.isna(rating):
        return 0
    rating = float(rating)
    if rating >= 5.5:
        return 0
    if rating >= 4.5:
        return 0
    if rating >= 3.5:
        return 1
    if rating >= 2.5:
        return 2
    if rating >= 1.5:
        return 3
    return 5
