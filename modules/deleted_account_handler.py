"""
Handle detection and processing of deleted accounts.
Accounts marked as "Account Deleted" with status "Closed" need special handling.
"""
import pandas as pd
import logging
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)


def identify_deleted_accounts(account_df: pd.DataFrame) -> List[str]:
    """
    Identify accounts that have been deleted in the platform.
    
    Args:
        account_df: DataFrame containing account data with columns:
                   - Id: Account ID
                   - AccountName: Account name (will be "Account Deleted" for deleted accounts)
                   - AccountStatus: Account status (will be "Closed" for deleted accounts)
    
    Returns:
        List of account IDs that are marked as deleted
    """
    if account_df.empty:
        return []
    
    # Check if required columns exist
    required_cols = ['AccountName', 'AccountStatus', 'Id']
    missing_cols = [col for col in required_cols if col not in account_df.columns]
    
    if missing_cols:
        logger.warning(f"Missing columns for deleted account detection: {missing_cols}")
        return []
    
    # Find deleted accounts
    deleted_mask = (
        (account_df['AccountName'] == 'Account Deleted') & 
        (account_df['AccountStatus'] == 'Closed')
    )
    
    deleted_accounts = account_df[deleted_mask]
    
    if not deleted_accounts.empty:
        # Convert IDs to strings
        deleted_ids = deleted_accounts['Id'].astype(str).tolist()
        logger.info(f"Identified {len(deleted_ids)} deleted accounts")
        return deleted_ids
    
    return []


def filter_deleted_accounts(df: pd.DataFrame, deleted_account_ids: List[str], 
                          id_column: str = 'Account_Name') -> pd.DataFrame:
    """
    Filter out deleted accounts from a DataFrame.
    
    Args:
        df: DataFrame to filter
        deleted_account_ids: List of account IDs to remove
        id_column: Name of the column containing account IDs (default: 'Account_Name')
    
    Returns:
        DataFrame with deleted accounts removed
    """
    if not deleted_account_ids or df.empty:
        return df
    
    if id_column not in df.columns:
        logger.warning(f"Column '{id_column}' not found in DataFrame. No filtering applied.")
        return df
    
    initial_count = len(df)
    filtered_df = df[~df[id_column].astype(str).isin(deleted_account_ids)]
    removed_count = initial_count - len(filtered_df)
    
    if removed_count > 0:
        logger.info(f"Filtered out {removed_count} deleted accounts from DataFrame")
    
    return filtered_df


def process_deleted_accounts(account_df: pd.DataFrame, 
                           token: str,
                           delete_from_zoho_func: callable) -> Tuple[List[str], Optional[dict]]:
    """
    Process deleted accounts: identify them and optionally delete from Zoho.
    
    Args:
        account_df: DataFrame containing account data
        token: Zoho API token (if None, only identifies but doesn't delete)
        delete_from_zoho_func: Function to call for Zoho deletion
    
    Returns:
        Tuple of (deleted_account_ids, deletion_results)
        deletion_results will be None if no token provided
    """
    # Identify deleted accounts
    deleted_account_ids = identify_deleted_accounts(account_df)
    
    if not deleted_account_ids:
        return [], None
    
    print(f"\nFound {len(deleted_account_ids)} deleted accounts")
    
    # Delete from Zoho if token provided
    deletion_results = None
    if token and delete_from_zoho_func:
        try:
            print(f"Deleting {len(deleted_account_ids)} accounts from Zoho...")
            deletion_results = delete_from_zoho_func(token, deleted_account_ids)
            
            if deletion_results:
                print(f"Successfully deleted: {deletion_results.get('successful', 0)} accounts")
                if deletion_results.get('failed', 0) > 0:
                    print(f"Failed to delete: {deletion_results.get('failed', 0)} accounts")
        except Exception as e:
            logger.error(f"Failed to delete accounts from Zoho: {str(e)}")
            print(f"ERROR: Failed to delete accounts from Zoho: {str(e)}")
    
    return deleted_account_ids, deletion_results


def should_exclude_from_calculations(account_df: pd.DataFrame) -> pd.Series:
    """
    Create a boolean mask for accounts that should be excluded from calculations.
    
    This is useful when you want to completely exclude deleted accounts from
    metrics calculations (as opposed to including them but not syncing to Zoho).
    
    Args:
        account_df: DataFrame containing account data
    
    Returns:
        Boolean Series where True indicates the account should be excluded
    """
    if account_df.empty:
        return pd.Series(False, index=account_df.index)
    
    # Check if required columns exist
    if 'AccountName' not in account_df.columns or 'AccountStatus' not in account_df.columns:
        return pd.Series(False, index=account_df.index)
    
    # Mark deleted accounts for exclusion
    exclude_mask = (
        (account_df['AccountName'] == 'Account Deleted') & 
        (account_df['AccountStatus'] == 'Closed')
    )
    
    return exclude_mask