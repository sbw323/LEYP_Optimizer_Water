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

    def run_year(self, network: list, year: int) -> dict[str, Any]:
        """
        Execute annual replacement decisions within budget constraints.
        
        Args:
            network: List of Pipe objects
            year: Current simulation year
            
        Returns:
            Dictionary with keys 'Year', 'Spend', 'Count'
        """
        # Filter eligible pipes (condition <= threshold, not failed)
        eligible_pipes = []
        for pipe in network:
            if (pipe.current_condition <= self.rehab_trigger and
                pipe.current_condition > 1.001):  # Exclude failed pipes
                eligible_pipes.append(pipe)

        # Sort by annualized risk (highest first)
        eligible_pipes.sort(key=self.get_annualized_risk, reverse=True)

        # Execute replacements within budget
        total_spend = 0.0
        replacement_count = 0

        for pipe in eligible_pipes:
            cost = self.calculate_cost(pipe)
            if total_spend + cost <= self.budget:
                self.execute_replacement(pipe, year)
                total_spend += cost
                replacement_count += 1
            else:
                break  # Budget exhausted

        return {
            'Year': year,
            'Spend': total_spend,
            'Count': replacement_count
        }

    def execute_replacement(self, pipe, year: int) -> None:
        """
        Execute pipe replacement and log action.
        
        Args:
            pipe: Pipe object to replace
            year: Current simulation year
        """
        # Record pre-replacement state for logging
        pre_condition = pipe.current_condition
        original_material = pipe.material
        cost = self.calculate_cost(pipe)

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

        # Calculate priority (annualized risk used for ranking)
        priority = self.get_annualized_risk(pipe)

        # Log the action
        self.action_log.append({
            'Year': year,
            'PipeID': pipe.id,
            'Action': ACTION_CIP_REPLACEMENT,  # Use constant from config
            'PreCondition': pre_condition,
            'PostCondition': 6.0,
            'Condition_Before': pre_condition,  # Alias for validator compatibility
            'Priority': priority,  # Annualized risk used for ranking
            'Material': original_material,  # Original material
            'NewMaterial': self.replacement_material,  # Replacement material
            'Cost': cost,
            'Length': pipe.length,
            'Diameter': pipe.diameter
        })
