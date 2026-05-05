"""
Booking data aggregation business logic.

This module handles all the business logic for processing booking transactions,
including deduplication, metric calculations, and period-based aggregations.
"""
import pandas as pd
import logging
from typing import Dict, Iterator, Set, Optional, Any
from datetime import date

logger = logging.getLogger(__name__)


class BookingAggregator:
    """Handles aggregation of booking transaction data into account metrics."""
    
    def __init__(self, cutoff_365: date, cutoff_730: date,
                 event_freq_cutoff_current: date, event_freq_cutoff_previous: date,
                 skip_event_metrics: bool = False):
        """
        Initialize aggregator with date cutoffs.

        Args:
            cutoff_365: Cutoff date for current period (365 days ago)
            cutoff_730: Cutoff date for previous period (730 days ago)
            event_freq_cutoff_current: Cutoff for current event frequency period
            event_freq_cutoff_previous: Cutoff for previous event frequency period
            skip_event_metrics: When True, event-related fields default to empty
                instead of being computed. The v2 tier calculator doesn't use
                them, so the historical-rebuild path sets this for a large
                speedup. Production daily run leaves this False (the v1 path
                via account_processor.py needs them).
        """
        self.cutoff_365 = cutoff_365
        self.cutoff_730 = cutoff_730
        self.event_freq_cutoff_current = event_freq_cutoff_current
        self.event_freq_cutoff_previous = event_freq_cutoff_previous
        self.skip_event_metrics = skip_event_metrics
        self.processed_chunks = 0
        self.total_rows = 0

        # OPTIMIZATION: Accumulate all data for bulk processing instead of dict updates
        self.accumulated_chunks = []
        
    def process_chunk(self, chunk: pd.DataFrame) -> None:
        """
        OPTIMIZED: Prepare chunk and accumulate for bulk processing.
        Eliminates expensive row-by-row operations.
        
        Args:
            chunk: DataFrame chunk containing booking transactions
        """
        if chunk.empty:
            return
            
        # OPTIMIZATION: Vectorized preparation - all calculations at once
        chunk = self._prepare_chunk_vectorized(chunk)
        
        # OPTIMIZATION: Just accumulate - no expensive dict operations
        self.accumulated_chunks.append(chunk)
        
        self.processed_chunks += 1
        self.total_rows += len(chunk)
        
        # Log progress every 10 chunks
        if self.processed_chunks % 10 == 0:
            logger.info(f"Processed {self.processed_chunks} chunks "
                       f"({self.total_rows:,} rows)")
    
    def _prepare_chunk_vectorized(self, chunk: pd.DataFrame) -> pd.DataFrame:
        """
        OPTIMIZED: Vectorized chunk preparation with all calculations.
        Replaces expensive row-by-row operations.
        """
        # Ensure fee columns exist and handle nulls
        fee_columns = ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']
        for col in fee_columns:
            if col not in chunk.columns:
                chunk[col] = 0.0
            else:
                chunk[col] = chunk[col].fillna(0.0)
        
        # Vectorized calculations
        chunk['Revenue'] = (chunk['BookingFee'] + chunk['CardFee'] + 
                           chunk['ProcessingFee'] + chunk['TicketFee'])
        chunk['Year'] = chunk['TransactionDate'].dt.year
        chunk['tx_date'] = chunk['TransactionDate'].dt.date
        
        # Period classification flags for efficient filtering later
        chunk['is_current'] = chunk['tx_date'] >= self.cutoff_365
        chunk['is_previous'] = (chunk['tx_date'] >= self.cutoff_730) & (~chunk['is_current'])
        chunk['is_freq_current'] = chunk['tx_date'] >= self.event_freq_cutoff_current
        chunk['is_freq_previous'] = ((chunk['tx_date'] >= self.event_freq_cutoff_previous) & 
                                    (~chunk['is_freq_current']))
        
        # Drop duplicates
        chunk = chunk.drop_duplicates(subset='BookingTransactionId')
        
        # Filter to only successful transactions, excluding Failed and Unknown
        if 'Status' in chunk.columns:
            chunk = chunk[chunk['Status'] == 'Successful']
        
        return chunk
    
    def finalize_metrics(self) -> Dict[int, Dict[str, Any]]:
        """
        OPTIMIZED: Bulk aggregation using vectorized pandas operations.
        Replaces expensive dictionary updates with fast groupby operations.
        
        Returns:
            Dictionary of account metrics
        """
        if not self.accumulated_chunks:
            logger.info("No data to process")
            return {}
        
        start_time = pd.Timestamp.now()
        # Demoted to debug — fires once per replay pass during a 4k+ day
        # historical rebuild and adds nothing for the production daily run.
        logger.debug("Starting vectorized bulk aggregation...")
        
        # OPTIMIZATION: Combine all chunks into single DataFrame for bulk processing.
        # Skip the concat when only one usable chunk is fed — common in the
        # historical replay path where the entire date-filtered slice is passed
        # as a single chunk. pd.concat on one frame is pure overhead.
        non_empty_chunks = [
            chunk for chunk in self.accumulated_chunks
            if not chunk.empty and not chunk.isna().all().all()
        ]
        if not non_empty_chunks:
            logger.warning("All chunks are empty")
            return {}
        if len(non_empty_chunks) == 1:
            all_data = non_empty_chunks[0]
        else:
            logger.info("Combining accumulated chunks...")
            all_data = pd.concat(non_empty_chunks, ignore_index=True)
            logger.info(f"Combined {len(all_data):,} rows from {len(self.accumulated_chunks)} chunks")
        
        # Clear accumulated chunks to free memory
        self.accumulated_chunks = []
        
        # OPTIMIZATION: Vectorized aggregations using pandas groupby (much faster)
        logger.debug("Computing vectorized aggregations...")
        
        # Basic lifetime metrics. Round only the numeric columns —
        # pandas warns (and will eventually error) on .round() against
        # datetime/timedelta dtypes, which TransactionDate min/max are.
        # 'nunique' is the vectorised C-level equivalent of len(set(x)) and
        # is meaningfully faster across many groups (matters during the
        # historical rebuild where this runs once per day in the daterange).
        basic_agg = all_data.groupby('AccountId').agg({
            'TicketQuantity': 'sum',
            'Revenue': 'sum',
            'Year': 'nunique',
            'TransactionDate': ['min', 'max']
        })
        basic_agg.columns = ['tickets_lifetime', 'revenue_lifetime', 'years_loyalty',
                            'first_booking_date', 'last_booking_date']
        basic_agg[['tickets_lifetime', 'revenue_lifetime']] = (
            basic_agg[['tickets_lifetime', 'revenue_lifetime']].round(2)
        )
        
        # Current period metrics
        current_data = all_data[all_data['is_current']]
        current_agg = current_data.groupby('AccountId').agg({
            'TicketQuantity': 'sum',
            'Revenue': 'sum'
        }).round(2)
        current_agg.columns = ['tickets_current', 'revenue_current']
        
        # Previous period metrics  
        previous_data = all_data[all_data['is_previous']]
        previous_agg = previous_data.groupby('AccountId').agg({
            'TicketQuantity': 'sum',
            'Revenue': 'sum'
        }).round(2)
        previous_agg.columns = ['tickets_prev', 'revenue_prev']
        
        # Previous period years — vectorised nunique instead of a Python lambda.
        pre_cutoff_data = all_data[all_data['tx_date'] < self.cutoff_365]
        years_prev_agg = pre_cutoff_data.groupby('AccountId')['Year'].nunique(
        ).to_frame('years_loyalty_prev')
        
        # Event metrics — skipped on rebuild paths since the v2 calculator
        # doesn't consume them.
        if self.skip_event_metrics:
            event_metrics: Dict[int, Dict[str, Any]] = {}
        else:
            event_metrics = self._calculate_event_metrics_vectorized(all_data)

        # Combine the four aggregate frames into a single per-account DataFrame
        # via outer joins, then materialise the dict-of-dicts shape with one
        # to_dict('index') call. This replaces the previous per-account Python
        # loop, which paid an O(N) .loc lookup for every account every call —
        # fine for one daily pass but the dominant cost across thousands of
        # historical-replay passes.
        logger.debug("Combining aggregated results...")
        combined = (
            basic_agg
            .join(current_agg, how='outer')
            .join(previous_agg, how='outer')
            .join(years_prev_agg, how='outer')
        )

        # Fill numeric defaults; preserve NaT for the date columns.
        numeric_defaults = {
            'tickets_lifetime': 0, 'revenue_lifetime': 0.0, 'years_loyalty': 0,
            'tickets_current': 0, 'revenue_current': 0.0,
            'tickets_prev': 0, 'revenue_prev': 0.0,
            'years_loyalty_prev': 0,
        }
        for col, default in numeric_defaults.items():
            if col in combined.columns:
                combined[col] = combined[col].fillna(default)

        # Derived metrics (vectorised).
        years = combined['years_loyalty'].astype(float)
        revenue_lifetime = combined['revenue_lifetime'].astype(float)
        revenue_current = combined['revenue_current'].astype(float)
        years_prev = combined['years_loyalty_prev'].astype(float)

        avg_revenue_per_year = pd.Series(0.0, index=combined.index)
        mask = years > 0
        avg_revenue_per_year.loc[mask] = (revenue_lifetime[mask] / years[mask])

        revenue_up_to_prev = revenue_lifetime - revenue_current
        avg_revenue_prev = pd.Series(0.0, index=combined.index)
        mask_prev = years_prev > 0
        avg_revenue_prev.loc[mask_prev] = (revenue_up_to_prev[mask_prev] / years_prev[mask_prev])

        combined['avg_revenue_per_year'] = avg_revenue_per_year.round(2)
        combined['avg_revenue_prev'] = avg_revenue_prev.round(2)
        combined['seen_tx_ids'] = 0  # Legacy field, kept for downstream contract

        # Lock in the consumer-facing dtypes. fillna can have promoted ints
        # to float, and synthetic / round-number inputs can leave revenue as
        # int — the original code forced these casts per row, so we do the
        # same once across the column.
        for int_col in ('tickets_lifetime', 'years_loyalty', 'tickets_current',
                        'tickets_prev', 'years_loyalty_prev'):
            if int_col in combined.columns:
                combined[int_col] = combined[int_col].astype(int)
        for float_col in ('revenue_lifetime', 'revenue_current', 'revenue_prev'):
            if float_col in combined.columns:
                combined[float_col] = combined[float_col].astype(float)

        # Materialise to dict-of-dicts (the contract every consumer expects).
        final_metrics: Dict[int, Dict[str, Any]] = combined.to_dict(orient='index')

        # Splice in event metrics. The empty-defaults pattern matches the
        # previous behaviour: every account in final_metrics gets the five
        # event-metric keys, even if event_metrics is empty (skip path or
        # missing columns).
        empty_event_defaults = {
            'event_months_current': set(),
            'event_months_previous': set(),
            'event_months_freq_current': set(),
            'event_months_freq_previous': set(),
            'event_creation_info': {},
        }
        for account_id, metrics in final_metrics.items():
            metrics.update(event_metrics.get(account_id, empty_event_defaults))

        elapsed = (pd.Timestamp.now() - start_time).total_seconds()
        rate = self.total_rows / elapsed if elapsed > 0 else 0

        logger.debug(f"Vectorized aggregation complete: {len(final_metrics):,} accounts in {elapsed:.1f}s "
                    f"({rate:.0f} rows/sec)")

        return final_metrics
    
    def _calculate_event_metrics_vectorized(self, all_data: pd.DataFrame) -> Dict[int, Dict[str, Any]]:
        """
        OPTIMIZED: Calculate event metrics using vectorized operations.
        """
        if 'EventId' not in all_data.columns or 'EventDate' not in all_data.columns:
            logger.info("EventId or EventDate columns missing - skipping event metrics")
            return {}
        
        # Filter to valid event data
        event_data = all_data[pd.notna(all_data['EventDate'])].copy()
        if event_data.empty:
            return {}
        
        # Add year-month tuples vectorized
        event_data['event_year_month'] = list(zip(
            event_data['EventDate'].dt.year,
            event_data['EventDate'].dt.month
        ))
        
        # Group by account and use vectorized aggregations
        event_metrics = {}
        
        for account_id, group in event_data.groupby('AccountId'):
            # Use boolean indexing for period filtering
            current_months = set(group[group['is_current']]['event_year_month'].unique())
            previous_months = set(group[group['is_previous']]['event_year_month'].unique())
            freq_current_months = set(group[group['is_freq_current']]['event_year_month'].unique())
            freq_previous_months = set(group[group['is_freq_previous']]['event_year_month'].unique())
            
            # Event creation info (vectorized per event)
            event_creation_info = {}
            if pd.notna(group['EventId']).any():
                for event_id, event_group in group.groupby('EventId'):
                    if pd.notna(event_id):
                        first_booking = event_group['TransactionDate'].min()
                        event_date = event_group['EventDate'].iloc[0]
                        event_date_clean = event_date.date() if hasattr(event_date, 'date') else event_date
                        first_booking_clean = first_booking.date() if hasattr(first_booking, 'date') else first_booking
                        lead_days = (event_date_clean - first_booking_clean).days
                        
                        event_creation_info[int(event_id)] = {
                            'first_booking': first_booking,
                            'event_date': event_date,
                            'lead_days': max(lead_days, 0)
                        }
            
            event_metrics[account_id] = {
                'event_months_current': current_months,
                'event_months_previous': previous_months,
                'event_months_freq_current': freq_current_months,
                'event_months_freq_previous': freq_previous_months,
                'event_creation_info': event_creation_info
            }
        
        return event_metrics
    
    def aggregate_bookings(self, chunks_iterator: Iterator[pd.DataFrame]) -> Dict[int, Dict[str, Any]]:
        """
        Main entry point to aggregate booking data from chunks.
        
        Args:
            chunks_iterator: Iterator yielding DataFrame chunks
            
        Returns:
            Dictionary of aggregated account metrics
        """
        logger.debug("Starting booking data aggregation...")
        start_time = pd.Timestamp.now()

        for chunk in chunks_iterator:
            self.process_chunk(chunk)

        elapsed = (pd.Timestamp.now() - start_time).total_seconds()
        rate = self.total_rows / elapsed if elapsed > 0 else 0

        logger.debug(f"Aggregation complete: {self.total_rows:,} rows in {elapsed:.1f}s "
                    f"({rate:.0f} rows/sec)")
        
        return self.finalize_metrics()