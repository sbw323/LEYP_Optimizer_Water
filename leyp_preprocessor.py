import math
import sys

import pandas as pd

from leyp_config import COLUMN_MAP, REAL_DATA_PATH

# Configuration
TARGET_SEGMENT_LENGTH = 25.0
DEFAULT_OUTPUT_FILENAME = "Louisa_LEYP_Optimized_Input.csv"

def preprocess_network(input_path=None, output_path=None, skip_segmentation=False):
    """
    Standardizes and segments pipe data.
    
    Args:
        input_path (str): Path to source CSV.
        output_path (str): Path to destination CSV.
        skip_segmentation (bool): If True, passes data through without modification.
    """
    # Default to config if no arguments passed
    target_input = input_path if input_path else REAL_DATA_PATH
    target_output = output_path if output_path else DEFAULT_OUTPUT_FILENAME

    mode_label = "Pass-Through (No Segmentation)" if skip_segmentation else f"Segmentation ({TARGET_SEGMENT_LENGTH}ft)"
    print(f"--- Preprocessor [{mode_label}]: Reading {target_input} -> Writing {target_output} ---")

    try:
        df = pd.read_csv(target_input)
    except FileNotFoundError:
        print(f"Error: Could not find file {target_input}")
        return

    # --- MODE: PASS-THROUGH ---
    if skip_segmentation:
        print("  Skipping segmentation logic...")
        # We simply save the file to the target location to maintain pipeline flow
        df.to_csv(target_output, index=False)
        print(f"  Saved raw data to {target_output}. Count: {len(df)} assets.")
        return

    # --- MODE: SEGMENTATION ---
    # 1. Standardize Headers (Map to Internal Names)
    # We create a copy to avoid SettingWithCopy warnings if reusing the df
    df_proc = df.copy()
    df_proc.rename(columns=COLUMN_MAP, inplace=True)

    # 2. Group by Covariates (The "Class Sum")
    group_cols = ['Material', 'Diameter', 'Age', 'Condition']

    # Ensure columns exist before grouping
    missing = [col for col in group_cols if col not in df_proc.columns]
    if missing:
        print(f"  [Error] Missing columns for grouping: {missing}")
        return

    grouped = df_proc.groupby(group_cols)['Length'].sum().reset_index()

    # 3. Segment Classes
    new_rows = []
    for idx, row in grouped.iterrows():
        total_class_len = row['Length']
        n_segments = math.ceil(total_class_len / TARGET_SEGMENT_LENGTH)
        if n_segments < 1: n_segments = 1
        actual_seg_len = total_class_len / n_segments

        for i in range(n_segments):
            segment = {
                'PipeID': f"Class_{idx}_Seg_{i+1}",
                'Material': row['Material'],
                'Diameter': row['Diameter'],
                'Age': row['Age'],
                'Condition': row['Condition'],
                'Length': actual_seg_len
            }
            new_rows.append(segment)

    optimized_df = pd.DataFrame(new_rows)

    # 4. Restore Headers (Map back to Input Format)
    reverse_map = {v: k for k, v in COLUMN_MAP.items()}
    optimized_df.rename(columns=reverse_map, inplace=True)

    optimized_df.to_csv(target_output, index=False)
    print(f"  Saved optimized data ({len(optimized_df)} segments).")

if __name__ == "__main__":
    # Allow manual triggering of skip mode via CLI:
    # python leyp_preprocessor.py --skip
    user_skip = False
    if len(sys.argv) > 1 and '--skip' in sys.argv:
        user_skip = True

    preprocess_network(skip_segmentation=user_skip)
