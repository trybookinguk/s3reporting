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
                them, so the historical-rebuild path sets this. Production
                daily run leaves this False (the v1 path via account_processor.py
                needs them).
        """
        self.cutoff_365 = cutoff_365
        self.cutoff_730 = cutoff_730
        self.event_freq_cutoff_current = event_freq_cutoff_current
        self.event_freq_cutoff_previous = event_freq_cutoff_previous
        self.skip_event_metrics = skip_event_metrics
        self.processed_chunks = 0
        self.total_rows = 0

        # Per-account accumulators. These hold at most one row per account
        # (~20k), never per-transaction (~4.6M) — so memory stays bounded no
        # matter how many millions of rows stream through. Sums stay UNROUNDED
        # here; rounding happens once in finalize (round-after-total-sum, to
        # match the original single-bulk-groupby behaviour exactly).
        #
        # Numeric sums: dict[AccountId] -> float
        self._tickets_lifetime: Dict[Any, float] = {}
        self._revenue_lifetime: Dict[Any, float] = {}
        self._tickets_current: Dict[Any, float] = {}
        self._revenue_current: Dict[Any, float] = {}
        self._tickets_prev: Dict[Any, float] = {}
        self._revenue_prev: Dict[Any, float] = {}
        # Date min/max: dict[AccountId] -> Timestamp
        self._first_booking: Dict[Any, Any] = {}
        self._last_booking: Dict[Any, Any] = {}
        # Distinct-year sets (nunique == len of union across chunks)
        self._years: Dict[Any, set] = {}
        self._years_prev: Dict[Any, set] = {}
        # Event accumulators (only populated when not skip_event_metrics)
        self._event_months_current: Dict[Any, set] = {}
        self._event_months_previous: Dict[Any, set] = {}
        self._event_months_freq_current: Dict[Any, set] = {}
        self._event_months_freq_previous: Dict[Any, set] = {}
        # event_creation_info: dict[AccountId] -> dict[EventId] -> {first_booking, event_date}
        self._event_creation: Dict[Any, Dict[int, Dict[str, Any]]] = {}

    def process_chunk(self, chunk: pd.DataFrame) -> None:
        """
        Prepare a chunk and fold its per-account aggregates into the running
        accumulators. Never retains the raw rows — peak memory is one chunk.

        Args:
            chunk: DataFrame chunk containing booking transactions
        """
        if chunk.empty:
            return

        chunk = self._prepare_chunk_vectorized(chunk)
        if chunk.empty:
            return

        self._accumulate_basic(chunk)
        if not self.skip_event_metrics:
            self._accumulate_events(chunk)

        self.processed_chunks += 1
        self.total_rows += len(chunk)

        if self.processed_chunks % 10 == 0:
            logger.info(f"Processed {self.processed_chunks} chunks "
                       f"({self.total_rows:,} rows)")

    @staticmethod
    def _add_sums(target: Dict[Any, float], grouped) -> None:
        """Fold a per-account Series of sums into a running dict (unrounded)."""
        for aid, val in grouped.items():
            target[aid] = target.get(aid, 0.0) + float(val)

    @staticmethod
    def _merge_sets(target: Dict[Any, set], grouped) -> None:
        """Union a per-account Series of sets into a running dict."""
        for aid, s in grouped.items():
            if aid in target:
                target[aid].update(s)
            else:
                target[aid] = set(s)

    def _accumulate_basic(self, chunk: pd.DataFrame) -> None:
        """Fold lifetime + period sums, date min/max, and year-sets per account.

        Each groupby here is over the chunk only (≤ accounts-in-chunk rows out),
        so this is the same arithmetic the old single bulk groupby did, just
        split additively across chunks. Sums of sums == sum of all; min of mins
        == global min; union of year-sets == global distinct years.
        """
        g = chunk.groupby('AccountId')
        self._add_sums(self._tickets_lifetime, g['TicketQuantity'].sum())
        self._add_sums(self._revenue_lifetime, g['Revenue'].sum())

        # Date min/max folded element-wise.
        for aid, val in g['TransactionDate'].min().items():
            cur = self._first_booking.get(aid)
            if cur is None or val < cur:
                self._first_booking[aid] = val
        for aid, val in g['TransactionDate'].max().items():
            cur = self._last_booking.get(aid)
            if cur is None or val > cur:
                self._last_booking[aid] = val

        # Distinct years (lifetime) and distinct years pre-cutoff_365.
        self._merge_sets(self._years, g['Year'].agg(set))
        pre = chunk[chunk['tx_date'] < self.cutoff_365]
        if not pre.empty:
            self._merge_sets(self._years_prev, pre.groupby('AccountId')['Year'].agg(set))

        cur_rows = chunk[chunk['is_current']]
        if not cur_rows.empty:
            cg = cur_rows.groupby('AccountId')
            self._add_sums(self._tickets_current, cg['TicketQuantity'].sum())
            self._add_sums(self._revenue_current, cg['Revenue'].sum())

        prev_rows = chunk[chunk['is_previous']]
        if not prev_rows.empty:
            pg = prev_rows.groupby('AccountId')
            self._add_sums(self._tickets_prev, pg['TicketQuantity'].sum())
            self._add_sums(self._revenue_prev, pg['Revenue'].sum())

    def _accumulate_events(self, chunk: pd.DataFrame) -> None:
        """Fold the four event-month sets and event_creation_info per account.

        Mirrors _calculate_event_metrics_vectorized but applied per chunk and
        merged: set-union for the month buckets, min(first_booking) /
        first-seen(event_date) for event_creation_info. lead_days is derived
        once at finalize from the accumulated first_booking.
        """
        if 'EventId' not in chunk.columns or 'EventDate' not in chunk.columns:
            return
        event_data = chunk[pd.notna(chunk['EventDate'])]
        if event_data.empty:
            return

        ym = list(zip(event_data['EventDate'].dt.year.to_numpy(),
                      event_data['EventDate'].dt.month.to_numpy()))
        event_data = event_data.assign(event_year_month=ym)

        def _fold_months(target, mask_col):
            sub = event_data.loc[event_data[mask_col], ['AccountId', 'event_year_month']]
            if sub.empty:
                return
            self._merge_sets(target, sub.groupby('AccountId')['event_year_month'].agg(set))

        _fold_months(self._event_months_current, 'is_current')
        _fold_months(self._event_months_previous, 'is_previous')
        _fold_months(self._event_months_freq_current, 'is_freq_current')
        _fold_months(self._event_months_freq_previous, 'is_freq_previous')

        ev = event_data[pd.notna(event_data['EventId'])]
        if ev.empty:
            return
        grp = ev.groupby(['AccountId', 'EventId']).agg(
            first_booking=('TransactionDate', 'min'),
            event_date=('EventDate', 'first'),
        ).reset_index()
        for row in grp.itertuples(index=False):
            acc = self._event_creation.setdefault(row.AccountId, {})
            eid = int(row.EventId)
            existing = acc.get(eid)
            if existing is None:
                acc[eid] = {'first_booking': row.first_booking,
                            'event_date': row.event_date}
            elif row.first_booking < existing['first_booking']:
                existing['first_booking'] = row.first_booking
    
    def _prepare_chunk_vectorized(self, chunk: pd.DataFrame) -> pd.DataFrame:
        """
        Per-chunk preparation: ensure fee columns exist, derive Revenue,
        and add date helpers used downstream.
        """
        fee_columns = ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']
        for col in fee_columns:
            if col not in chunk.columns:
                chunk[col] = 0.0
            else:
                chunk[col] = chunk[col].fillna(0.0)

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
        
        # Drop duplicates *within the chunk*. This matches the original
        # behaviour. It is only fully correct when BookingTransactionId is
        # globally unique across the whole input — which both supported feeds
        # guarantee: the warehouse has a PRIMARY KEY on BookingTransactionId,
        # and the --combined reference path feeds an already-deduped frame.
        # A naive multi-file CSV concat with cross-chunk dupes would not be
        # safe here (a duplicate split across chunks would survive).
        chunk = chunk.drop_duplicates(subset='BookingTransactionId')
        
        # Filter to only successful transactions, excluding Failed and Unknown
        if 'Status' in chunk.columns:
            chunk = chunk[chunk['Status'] == 'Successful']
        
        return chunk
    
    def finalize_metrics(self) -> Dict[int, Dict[str, Any]]:
        """
        Build the per-account metrics dict from the running accumulators.

        Identical output contract to the previous single-bulk-groupby
        implementation, but assembled from the per-account accumulators folded
        in process_chunk — so this never materialises the full row set.

        Returns:
            Dictionary of account metrics
        """
        if not self._tickets_lifetime:
            logger.info("No data to process")
            return {}

        start_time = pd.Timestamp.now()
        logger.debug("Assembling aggregated results from accumulators...")

        # Lifetime frame, indexed by AccountId. Round the numeric sums HERE
        # (round-after-total-sum), matching the old behaviour where rounding
        # was applied once after the bulk groupby.
        index = pd.Index(list(self._tickets_lifetime.keys()), name='AccountId')
        basic_agg = pd.DataFrame(index=index)
        basic_agg['tickets_lifetime'] = pd.Series(self._tickets_lifetime).round(2)
        basic_agg['revenue_lifetime'] = pd.Series(self._revenue_lifetime).round(2)
        basic_agg['years_loyalty'] = pd.Series(
            {aid: len(s) for aid, s in self._years.items()}
        )
        basic_agg['first_booking_date'] = pd.Series(self._first_booking)
        basic_agg['last_booking_date'] = pd.Series(self._last_booking)

        current_agg = pd.DataFrame({
            'tickets_current': pd.Series(self._tickets_current).round(2),
            'revenue_current': pd.Series(self._revenue_current).round(2),
        })
        previous_agg = pd.DataFrame({
            'tickets_prev': pd.Series(self._tickets_prev).round(2),
            'revenue_prev': pd.Series(self._revenue_prev).round(2),
        })
        years_prev_agg = pd.DataFrame({
            'years_loyalty_prev': pd.Series(
                {aid: len(s) for aid, s in self._years_prev.items()}
            )
        })

        # Event metrics — skipped on rebuild paths since the v2 calculator
        # doesn't consume them.
        if self.skip_event_metrics:
            event_metrics: Dict[int, Dict[str, Any]] = {}
        else:
            event_metrics = self._finalize_event_metrics()

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

        # Derived metrics.
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

        logger.debug(f"Aggregation complete: {len(final_metrics):,} accounts in {elapsed:.1f}s "
                    f"({rate:.0f} rows/sec)")

        return final_metrics
    
    def _finalize_event_metrics(self) -> Dict[int, Dict[str, Any]]:
        """Assemble per-account event metrics from the running accumulators.

        Output shape per account (unchanged contract):
            {
              event_months_current: set[(year, month)],
              event_months_previous: set[(year, month)],
              event_months_freq_current: set[(year, month)],
              event_months_freq_previous: set[(year, month)],
              event_creation_info: {event_id: {first_booking, event_date, lead_days}},
            }

        lead_days is derived here from the accumulated first_booking and
        event_date: (event_date.date() - first_booking.date()).days, clamped
        >= 0, matching the original normalize()-based whole-day difference.
        """
        # Build event_creation_info with lead_days computed from accumulated
        # first_booking / event_date.
        event_creation_by_acc: Dict[int, Dict[int, Dict[str, Any]]] = {}
        for aid, events in self._event_creation.items():
            out_events = {}
            for eid, info in events.items():
                fb = info['first_booking']
                ed = info['event_date']
                lead = (ed.normalize() - fb.normalize()).days
                if lead < 0:
                    lead = 0
                out_events[eid] = {
                    'first_booking': fb,
                    'event_date': ed,
                    'lead_days': int(lead),
                }
            event_creation_by_acc[aid] = out_events

        all_accounts = (
            set(self._event_months_current) | set(self._event_months_previous)
            | set(self._event_months_freq_current) | set(self._event_months_freq_previous)
            | set(event_creation_by_acc)
        )
        event_metrics: Dict[int, Dict[str, Any]] = {}
        for aid in all_accounts:
            event_metrics[aid] = {
                'event_months_current': self._event_months_current.get(aid, set()),
                'event_months_previous': self._event_months_previous.get(aid, set()),
                'event_months_freq_current': self._event_months_freq_current.get(aid, set()),
                'event_months_freq_previous': self._event_months_freq_previous.get(aid, set()),
                'event_creation_info': event_creation_by_acc.get(aid, {}),
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