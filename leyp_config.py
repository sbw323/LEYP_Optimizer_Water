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
# Calibrated so a do-nothing run of this network (no CIP, no renewal, 100 yr)
# produces 0.287 breaks/mile/year.  Anchor: Folkman (2018) national averages
# weighted by this network's material mix give 0.077 breaks/mile/year across
# all ages; an untreated network that is already 60% past design life and ages
# a further century should sit several times above that.  Re-derive this value
# if the inventory, the material parameters, or ALPHA change.
HAZARD_LENGTH_SCALE = 1500.0
SIMULATION_YEARS = 100

# ==========================================
# 6. COST PARAMETERS
# ==========================================

# Annual Budget
ANNUAL_BUDGET = 50000

# Pipe failure replacement cost (legacy parameter)
GLOBAL_COST_PER_FT = 500

# Water Main Replacement Costs
CIP_REPLACEMENT_COST_PER_INCH_FT = 120.00
EMERGENCY_REPAIR_COST_PER_BREAK = 5000.00

# Emergency replacement carries a premium over planned CIP work: mobilisation,
# out-of-hours crews, service interruption, road reinstatement and the loss of
# any opportunity to upsize or co-ordinate with other works.  At $5,000/ft
# against CIP's $120/inch-ft this is roughly 6.9x for a 6-inch main and 3.5x
# for a 12-inch one, in line with the 2-5x range typical of utility practice.
# This rate is also the consequence term in ReplacementManager risk scoring;
# because it enters both risk and cost uniformly it scales the objectives
# without altering the replacement ranking.
EMERGENCY_REPLACEMENT_COST_PER_FT = 5000.00
DEFAULT_REPLACEMENT_MATERIAL = "HDPE"

# ==========================================
# 7. INTERVENTION TRIGGERS
# ==========================================
TRIGGERS = {"Rehab": 2.0}

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
