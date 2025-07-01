"""
Validation utilities for TryBooking reports.
"""
import os
import sys
from typing import List, Optional


def validate_environment_variables(required_vars: List[str], optional_vars: Optional[List[str]] = None):
    """
    Validate that required environment variables are set.
    
    Args:
        required_vars: List of required environment variable names
        optional_vars: List of optional environment variable names to check
    
    Raises:
        SystemExit: If any required variables are missing
    """
    missing = []
    for var in required_vars:
        if not os.environ.get(var):
            missing.append(var)
    
    if missing:
        print(f"\nERROR: Missing required environment variables:")
        for var in missing:
            print(f"  - {var}")
        print("\nPlease set these environment variables and try again.")
        sys.exit(1)
    
    # Check optional variables and warn if missing
    if optional_vars:
        missing_optional = []
        for var in optional_vars:
            if not os.environ.get(var):
                missing_optional.append(var)
        
        if missing_optional:
            print(f"\nWARNING: Optional environment variables not set:")
            for var in missing_optional:
                print(f"  - {var}")


def validate_dataframe_columns(df, required_columns: List[str], context: str = "DataFrame"):
    """
    Validate that a DataFrame has required columns.
    
    Args:
        df: pandas DataFrame to validate
        required_columns: List of required column names
        context: Description of the DataFrame for error messages
    
    Raises:
        ValueError: If any required columns are missing
    """
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        raise ValueError(f"{context} missing required columns: {missing}")


def safe_divide(numerator, denominator, default=0):
    """
    Safely divide two numbers, returning default if denominator is 0.
    
    Args:
        numerator: The numerator
        denominator: The denominator
        default: Value to return if denominator is 0
    
    Returns:
        Result of division or default value
    """
    if denominator == 0:
        return default
    return numerator / denominator