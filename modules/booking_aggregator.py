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
                 event_freq_cutoff_current: date, event_freq_cutoff_previous: date):
        """
        Initialize aggregator with date cutoffs.
        
        Args:
            cutoff_365: Cutoff date for current period (365 days ago)
            cutoff_730: Cutoff date for previous period (730 days ago)
            event_freq_cutoff_current: Cutoff for current event frequency period
            event_freq_cutoff_previous: Cutoff for previous event frequency period
        """
        self.cutoff_365 = cutoff_365
        self.cutoff_730 = cutoff_730
        self.event_freq_cutoff_current = event_freq_cutoff_current
        self.event_freq_cutoff_previous = event_freq_cutoff_previous
        self.account_metrics: Dict[int, Dict[str, Any]] = {}
        self.processed_chunks = 0
        self.total_rows = 0
        
    def process_chunk(self, chunk: pd.DataFrame) -> None:
        """
        Process a single chunk of booking data.
        
        Args:
            chunk: DataFrame chunk containing booking transactions
        """
        if chunk.empty:
            return
            
        # Ensure fee columns exist and have no nulls for revenue calculation
        fee_columns = ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']
        for col in fee_columns:
            if col not in chunk.columns:
                chunk[col] = 0.0
            else:
                chunk[col] = chunk[col].fillna(0.0)
        
        # Calculate revenue column vectorized (faster than apply)
        chunk['Revenue'] = (chunk['BookingFee'] + 
                           chunk['CardFee'] + 
                           chunk['ProcessingFee'] + 
                           chunk['TicketFee'])
        chunk['Year'] = chunk['TransactionDate'].dt.year
        
        # Drop duplicates within chunk
        chunk = chunk.drop_duplicates(subset='BookingTransactionId')
        
        # Aggregate by account
        for account_id, group in chunk.groupby('AccountId'):
            if account_id not in self.account_metrics:
                self.account_metrics[account_id] = self._initialize_account_metrics()
            
            self._update_account_metrics(account_id, group)
        
        self.processed_chunks += 1
        self.total_rows += len(chunk)
        
        # Log progress every 10 chunks
        if self.processed_chunks % 10 == 0:
            logger.info(f"Processed {self.processed_chunks} chunks "
                       f"({self.total_rows:,} rows, {len(self.account_metrics):,} accounts)")
    
    def _initialize_account_metrics(self) -> Dict[str, Any]:
        """Initialize empty metrics dictionary for an account."""
        return {
            'tickets_current': 0,
            'revenue_current': 0.0,
            'tickets_prev': 0,
            'revenue_prev': 0.0,
            'tickets_lifetime': 0,
            'revenue_lifetime': 0.0,
            'years': set(),
            'years_pre_cutoff': set(),
            'seen_tx_ids': set(),
            'event_months_current': set(),
            'event_months_previous': set(),
            'event_months_freq_current': set(),
            'event_months_freq_previous': set(),
            'event_creation_info': {},
            'last_booking_date': None,
            'first_booking_date': None
        }
    
    def _update_account_metrics(self, account_id: int, transactions: pd.DataFrame) -> None:
        """
        Update metrics for a specific account with new transactions.
        
        Args:
            account_id: Account identifier
            transactions: DataFrame of transactions for this account
        """
        metrics = self.account_metrics[account_id]
        
        # Filter out already seen transactions
        new_tx_mask = ~transactions['BookingTransactionId'].isin(metrics['seen_tx_ids'])
        new_transactions = transactions[new_tx_mask]
        
        if len(new_transactions) == 0:
            return
            
        # Update seen transaction IDs
        metrics['seen_tx_ids'].update(new_transactions['BookingTransactionId'].tolist())
        
        # Process each new transaction
        for _, tx in new_transactions.iterrows():
            self._process_transaction(metrics, tx)
        
        # Process event data if available
        if 'EventId' in new_transactions.columns and 'EventDate' in new_transactions.columns:
            self._process_event_data(account_id, new_transactions)
    
    def _process_transaction(self, metrics: Dict[str, Any], tx: pd.Series) -> None:
        """Process a single transaction and update metrics."""
        tx_date = tx['TransactionDate'].date()
        
        # Update lifetime metrics
        metrics['tickets_lifetime'] += tx['TicketQuantity']
        metrics['revenue_lifetime'] += tx['Revenue']
        metrics['years'].add(tx['Year'])
        
        # Update period-specific metrics
        if tx_date >= self.cutoff_365:
            metrics['tickets_current'] += tx['TicketQuantity']
            metrics['revenue_current'] += tx['Revenue']
        elif tx_date >= self.cutoff_730:
            metrics['tickets_prev'] += tx['TicketQuantity']
            metrics['revenue_prev'] += tx['Revenue']
        
        if tx_date < self.cutoff_365:
            metrics['years_pre_cutoff'].add(tx['Year'])
        
        # Update booking dates
        if metrics['last_booking_date'] is None or tx['TransactionDate'] > metrics['last_booking_date']:
            metrics['last_booking_date'] = tx['TransactionDate']
        if metrics['first_booking_date'] is None or tx['TransactionDate'] < metrics['first_booking_date']:
            metrics['first_booking_date'] = tx['TransactionDate']
    
    def _process_event_data(self, account_id: int, transactions: pd.DataFrame) -> None:
        """Process event-related data for frequency and lead time calculations."""
        event_data = transactions[['EventId', 'TransactionDate', 'EventDate']].copy()
        event_data = event_data[pd.notna(event_data['EventDate'])]
        
        if len(event_data) == 0:
            return
            
        metrics = self.account_metrics[account_id]
        
        # Extract year-month tuples from EventDate
        event_data['event_year_month'] = event_data['EventDate'].apply(
            lambda x: (x.year, x.month)
        )
        
        # Classify into periods for tier calculations
        current_mask = event_data['TransactionDate'].dt.date >= self.cutoff_365
        previous_mask = (event_data['TransactionDate'].dt.date >= self.cutoff_730) & (~current_mask)
        
        current_months = set(event_data[current_mask]['event_year_month'].unique())
        previous_months = set(event_data[previous_mask]['event_year_month'].unique())
        
        metrics['event_months_current'].update(current_months)
        metrics['event_months_previous'].update(previous_months)
        
        # For event frequency calculations
        freq_current_mask = event_data['TransactionDate'].dt.date >= self.event_freq_cutoff_current
        freq_previous_mask = (event_data['TransactionDate'].dt.date >= self.event_freq_cutoff_previous) & (~freq_current_mask)
        
        freq_current_months = set(event_data[freq_current_mask]['event_year_month'].unique())
        freq_previous_months = set(event_data[freq_previous_mask]['event_year_month'].unique())
        
        metrics['event_months_freq_current'].update(freq_current_months)
        metrics['event_months_freq_previous'].update(freq_previous_months)
        
        # Track event creation info for lead time calculations
        self._update_event_creation_info(account_id, event_data)
    
    def _update_event_creation_info(self, account_id: int, event_data: pd.DataFrame) -> None:
        """Update event creation information for lead time calculations."""
        metrics = self.account_metrics[account_id]
        event_groups = event_data[pd.notna(event_data['EventId'])].groupby('EventId')
        
        for event_id, group in event_groups:
            event_id_key = int(event_id) if pd.notna(event_id) else None
            if event_id_key and event_id_key not in metrics['event_creation_info']:
                first_booking = group['TransactionDate'].min()
                event_date = group['EventDate'].iloc[0]
                lead_days = (event_date.date() - first_booking.date()).days
                metrics['event_creation_info'][event_id_key] = {
                    'first_booking': first_booking,
                    'event_date': event_date,
                    'lead_days': max(lead_days, 0)
                }
    
    def finalize_metrics(self) -> Dict[int, Dict[str, Any]]:
        """
        Finalize metrics calculations and prepare for output.
        
        Returns:
            Dictionary of account metrics
        """
        logger.info("Finalizing account metrics...")
        
        for account_id, metrics in self.account_metrics.items():
            # Calculate derived metrics
            metrics['years_loyalty'] = len(metrics['years'])
            metrics['years_loyalty_prev'] = len(metrics['years_pre_cutoff'])
            
            # Calculate average revenue per year
            if metrics['years_loyalty'] > 0:
                metrics['avg_revenue_per_year'] = metrics['revenue_lifetime'] / metrics['years_loyalty']
            else:
                metrics['avg_revenue_per_year'] = 0
                
            if metrics['years_loyalty_prev'] > 0:
                # Revenue up to previous period
                revenue_up_to_prev = metrics['revenue_lifetime'] - metrics['revenue_current']
                metrics['avg_revenue_prev'] = revenue_up_to_prev / metrics['years_loyalty_prev']
            else:
                metrics['avg_revenue_prev'] = 0
            
            # Convert sets to counts for efficiency
            metrics['seen_tx_ids'] = len(metrics['seen_tx_ids'])
        
        logger.info(f"Finalized metrics for {len(self.account_metrics):,} accounts")
        return self.account_metrics
    
    def aggregate_bookings(self, chunks_iterator: Iterator[pd.DataFrame]) -> Dict[int, Dict[str, Any]]:
        """
        Main entry point to aggregate booking data from chunks.
        
        Args:
            chunks_iterator: Iterator yielding DataFrame chunks
            
        Returns:
            Dictionary of aggregated account metrics
        """
        logger.info("Starting booking data aggregation...")
        start_time = pd.Timestamp.now()
        
        for chunk in chunks_iterator:
            self.process_chunk(chunk)
        
        elapsed = (pd.Timestamp.now() - start_time).total_seconds()
        rate = self.total_rows / elapsed if elapsed > 0 else 0
        
        logger.info(f"Aggregation complete: {self.total_rows:,} rows in {elapsed:.1f}s "
                   f"({rate:.0f} rows/sec)")
        
        return self.finalize_metrics()