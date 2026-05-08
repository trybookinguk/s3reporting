"""
UK Regional Segmentation Module

Process UK postcodes and assign regions to accounts and events for
client segmentation and targeting.
"""

import pandas as pd
import numpy as np
import re
from typing import Dict, Tuple, Optional
import logging
import time

logger = logging.getLogger(__name__)

# Complete UK regional mapping
UK_REGIONS = {
    'London': frozenset(['E', 'EC', 'N', 'NW', 'SE', 'SW', 'W', 'WC']),
    'Scotland': frozenset(['AB', 'DD', 'DG', 'EH', 'FK', 'G', 'HS', 'IV', 'KA', 
                          'KW', 'KY', 'ML', 'PA', 'PH', 'TD', 'ZE']),
    'Wales': frozenset(['CF', 'LD', 'LL', 'NP', 'SA', 'SY']),
    'Northern Ireland': frozenset(['BT']),
    'North East': frozenset(['DH', 'DL', 'NE', 'SR', 'TS']),
    'North West': frozenset(['BB', 'BD', 'BL', 'CA', 'CH', 'CW', 'FY', 'L', 
                            'LA', 'M', 'OL', 'PR', 'SK', 'WA', 'WN']),
    'Yorkshire': frozenset(['DN', 'HD', 'HG', 'HU', 'HX', 'LS', 'S', 'WF', 'YO']),
    'East Midlands': frozenset(['DE', 'LE', 'LN', 'NG', 'NN', 'PE']),
    'West Midlands': frozenset(['B', 'CV', 'DY', 'HR', 'ST', 'TF', 'WR', 'WS', 'WV']),
    'East of England': frozenset(['AL', 'CB', 'CM', 'CO', 'EN', 'HP', 'IG', 
                                 'IP', 'LU', 'MK', 'NR', 'SG', 'SS']),
    'South East': frozenset(['BN', 'BR', 'CT', 'DA', 'GU', 'HA', 'KT', 'ME', 
                            'OX', 'PO', 'RG', 'RH', 'RM', 'SL', 'SM', 'SO', 'TN', 'TW', 'UB']),
    'South West': frozenset(['BA', 'BH', 'BS', 'DT', 'EX', 'GL', 'PL', 'SN', 
                            'SP', 'TA', 'TQ', 'TR']),
    'Channel Islands': frozenset(['GY', 'JE']),
    'Isle of Man': frozenset(['IM'])
}

# Create reverse mapping for efficient lookup
POSTCODE_TO_REGION = {}
for region, postcodes in UK_REGIONS.items():
    for postcode in postcodes:
        POSTCODE_TO_REGION[postcode] = region

# Set of all valid UK postcode areas for validation
VALID_UK_POSTCODE_AREAS = set(POSTCODE_TO_REGION.keys()) | {'BFPO'}


def is_valid_uk_postcode_area(area: str) -> bool:
    """Check if a postcode area is a valid UK postcode area."""
    if not area or not isinstance(area, str):
        return False
    return area.upper() in VALID_UK_POSTCODE_AREAS


def extract_postcode_areas_vectorized(postcodes: pd.Series) -> pd.Series:
    """
    Extract postcode areas from a series of postcodes.
    
    Args:
        postcodes: Series of postcode strings
        
    Returns:
        Series of postcode areas (e.g., 'SW', 'M1', etc.) or None for invalid
    """
    # Handle null values
    valid_mask = postcodes.notna() & (postcodes != '')
    
    # Initialize result with None (as object dtype to handle strings)
    result = pd.Series(None, index=postcodes.index, dtype='object')
    
    if valid_mask.any():
        # Clean postcodes: strip, uppercase
        clean_postcodes = postcodes[valid_mask].str.strip().str.upper()
        
        # Special case: BFPO postcodes
        bfpo_mask = clean_postcodes.str.startswith('BFPO')
        result.loc[valid_mask & bfpo_mask] = 'BFPO'
        
        # Extract regular postcode areas (1-2 letters at start)
        # Using extract to get the letter portion
        non_bfpo_mask = valid_mask & ~bfpo_mask
        if non_bfpo_mask.any():
            extracted = postcodes[non_bfpo_mask].str.strip().str.upper().str.extract(
                r'^([A-Z]{1,2})', expand=False
            )
            result.loc[non_bfpo_mask] = extracted
    
    return result


def get_regions_vectorized(postcode_areas: pd.Series) -> pd.Series:
    """
    Map postcode areas to regions.
    
    Args:
        postcode_areas: Series of postcode areas
        
    Returns:
        Series of region names
    """
    # Use map for efficient lookup, default to 'Unknown'
    regions = postcode_areas.map(POSTCODE_TO_REGION).fillna('Unknown')
    
    # Handle None/NaN postcode areas
    regions[postcode_areas.isna()] = 'Unknown'
    
    return regions


def assign_account_regions(accounts_df: pd.DataFrame, events_df: pd.DataFrame, 
                         batch_size: int = 50000) -> pd.DataFrame:
    """
    Assign regions to accounts with fallback logic.
    
    Args:
        accounts_df: DataFrame with account data including AccountPostcode
        events_df: DataFrame with event data including EventPostcode
        batch_size: Size of batches for processing (for memory efficiency)
        
    Returns:
        DataFrame with added columns: Region, Has_Postcode, Region_Source
    """
    start_time = time.time()
    total_accounts = len(accounts_df)
    logger.info(f"Starting regional assignment for {total_accounts:,} accounts")
    
    # Create a copy to avoid modifying original
    result_df = accounts_df.copy()
    
    # Track data quality - accounts data uses 'Postcode' column
    result_df['Has_Postcode'] = result_df['Postcode'].notna() & (result_df['Postcode'] != '')
    
    # Extract postcode areas and assign regions from account postcodes
    logger.info("Processing account postcodes...")
    account_areas = extract_postcode_areas_vectorized(result_df['Postcode'])
    result_df['Region'] = get_regions_vectorized(account_areas)
    result_df['Region_Source'] = 'No Data'
    
    # Mark accounts that got region from their postcode
    has_valid_region = (result_df['Region'] != 'Unknown') & result_df['Has_Postcode']
    result_df.loc[has_valid_region, 'Region_Source'] = 'Account'
    
    # For accounts without valid postcode, use event data
    missing_region_mask = result_df['Region'] == 'Unknown'
    accounts_needing_fallback = result_df[missing_region_mask]['Id'].unique()
    
    if len(accounts_needing_fallback) > 0:
        logger.info(f"Processing {len(accounts_needing_fallback):,} accounts using event postcodes...")
        
        # Process event postcodes once
        logger.info("Extracting postcode areas from events...")
        events_df = events_df.copy()
        events_df['event_area'] = extract_postcode_areas_vectorized(events_df['EventPostcode'])
        events_df['event_region'] = get_regions_vectorized(events_df['event_area'])
        
        # Filter to valid regions only
        valid_events = events_df[events_df['event_region'] != 'Unknown'].copy()
        
        if len(valid_events) > 0:
            # Group by account and find most common region
            logger.info("Calculating most common regions per account...")
            
            # Use value_counts approach for each account
            account_regions = (valid_events.groupby('AccountId')['event_region']
                             .agg(lambda x: x.value_counts().index[0] if len(x) > 0 else 'Unknown')
                             .reset_index())
            
            # Merge results back
            for _, row in account_regions.iterrows():
                account_mask = (result_df['Id'] == row['AccountId']) & missing_region_mask
                if account_mask.any():
                    result_df.loc[account_mask, 'Region'] = row['event_region']
                    result_df.loc[account_mask, 'Region_Source'] = 'Events'
    
    # Summary logging
    elapsed_time = time.time() - start_time
    accounts_with_region = (result_df['Region'] != 'Unknown').sum()
    logger.info(f"Regional assignment complete in {elapsed_time:.1f}s")
    logger.info(f"Accounts with region: {accounts_with_region:,}/{total_accounts:,} "
                f"({accounts_with_region/total_accounts*100:.1f}%)")
    
    # Log distribution
    region_counts = result_df['Region'].value_counts()
    logger.info("Regional distribution:")
    for region, count in region_counts.items():
        pct = (count / total_accounts) * 100
        logger.info(f"  {region}: {count:,} ({pct:.1f}%)")
    
    return result_df


def process_event_regions(events_df: pd.DataFrame) -> pd.DataFrame:
    """
    Add postcode area and region to events.
    
    Args:
        events_df: DataFrame with event data including EventPostcode
        
    Returns:
        DataFrame with added columns: PostcodeArea, Region
    """
    start_time = time.time()
    total_events = len(events_df)
    logger.info(f"Processing regions for {total_events:,} events")
    
    result_df = events_df.copy()
    
    # Extract postcode areas
    result_df['PostcodeArea'] = extract_postcode_areas_vectorized(result_df['EventPostcode'])
    
    # Get regions
    result_df['Region'] = get_regions_vectorized(result_df['PostcodeArea'])
    
    # Summary
    elapsed_time = time.time() - start_time
    events_with_region = (result_df['Region'] != 'Unknown').sum()
    logger.info(f"Event processing complete in {elapsed_time:.1f}s")
    logger.info(f"Events with valid region: {events_with_region:,}/{total_events:,} "
                f"({events_with_region/total_events*100:.1f}%)")
    
    return result_df


def generate_data_quality_summary(accounts_df: pd.DataFrame, events_df: pd.DataFrame, 
                                total_accounts: int = None, accounts_with_events: int = None) -> Dict:
    """
    Generate comprehensive data quality metrics.
    
    Args:
        accounts_df: Processed accounts DataFrame with regions (accounts with events or postcodes)
        events_df: Processed events DataFrame with regions
        total_accounts: Total number of accounts in the system (before filtering)
        accounts_with_events: Number of accounts that have events
        
    Returns:
        Dictionary with summary statistics
    """
    # Account metrics
    if total_accounts is None:
        total_accounts_all = len(accounts_df)
    else:
        total_accounts_all = total_accounts
    
    # Accounts analysed (with events or postcodes)
    accounts_analyzed = len(accounts_df)
    
    # Use provided count or default
    if accounts_with_events is None:
        accounts_with_events_count = accounts_analyzed
    else:
        accounts_with_events_count = accounts_with_events
    accounts_with_postcode = accounts_df['Has_Postcode'].sum()
    accounts_with_region = (accounts_df['Region'] != 'Unknown').sum()
    accounts_from_account = (accounts_df['Region_Source'] == 'Account').sum()
    accounts_from_events = (accounts_df['Region_Source'] == 'Events').sum()
    
    # Event metrics
    total_events = len(events_df)
    events_with_postcode = events_df['EventPostcode'].notna().sum()
    events_with_region = (events_df['Region'] != 'Unknown').sum()
    
    # Regional distribution
    region_dist = accounts_df['Region'].value_counts().to_dict()
    
    # Postcode area distribution (for accounts with postcodes)
    postcode_series = accounts_df[accounts_df['Has_Postcode']]['Postcode']
    postcode_areas = extract_postcode_areas_vectorized(postcode_series)
    # Filter to only valid UK postcode areas
    valid_postcode_areas = postcode_areas[postcode_areas.isin(VALID_UK_POSTCODE_AREAS)]
    postcode_area_dist = valid_postcode_areas.value_counts().to_dict()
    
    # Event postcode area distribution
    event_postcode_areas = events_df['PostcodeArea'].dropna()
    # Filter to only valid UK postcode areas
    valid_event_areas = event_postcode_areas[event_postcode_areas.isin(VALID_UK_POSTCODE_AREAS)]
    event_postcode_dist = valid_event_areas.value_counts().to_dict()
    
    summary = {
        'accounts': {
            'total_all': total_accounts_all,
            'with_events': accounts_with_events_count,
            'with_events_pct': (accounts_with_events_count / total_accounts_all * 100) if total_accounts_all > 0 else 0,
            'analyzed': accounts_analyzed,
            'analyzed_pct': (accounts_analyzed / total_accounts_all * 100) if total_accounts_all > 0 else 0,
            'with_postcode': accounts_with_postcode,
            'with_postcode_pct': (accounts_with_postcode / accounts_analyzed * 100) if accounts_analyzed > 0 else 0,
            'with_region': accounts_with_region,
            'with_region_pct': (accounts_with_region / accounts_analyzed * 100) if accounts_analyzed > 0 else 0,
            'from_account_postcode': accounts_from_account,
            'from_event_postcodes': accounts_from_events,
            'without_region': accounts_analyzed - accounts_with_region
        },
        'events': {
            'total': total_events,
            'with_postcode': events_with_postcode,
            'with_postcode_pct': (events_with_postcode / total_events * 100) if total_events > 0 else 0,
            'with_region': events_with_region,
            'with_region_pct': (events_with_region / total_events * 100) if total_events > 0 else 0
        },
        'regional_distribution': region_dist,
        'postcode_area_distribution': postcode_area_dist,
        'event_postcode_distribution': event_postcode_dist
    }
    
    return summary