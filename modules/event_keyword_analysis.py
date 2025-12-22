"""
Event Keyword and Timing Analysis Module

Extracts marketing intelligence from event data:
- Keyword extraction from event names
- Temporal patterns (when events happen, lead times)
- Keyword associations (co-occurring terms)
"""

import pandas as pd
import numpy as np
import re
from collections import Counter, defaultdict
from itertools import combinations
from typing import Dict, List, Tuple, Set, Optional
import calendar

# Common English stopwords to filter out
STOPWORDS = frozenset([
    'a', 'an', 'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'as', 'is', 'was', 'are', 'were', 'been',
    'be', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
    'could', 'should', 'may', 'might', 'must', 'shall', 'can', 'need',
    'this', 'that', 'these', 'those', 'it', 'its', 'they', 'their',
    'we', 'our', 'you', 'your', 'he', 'she', 'his', 'her', 'him',
    'i', 'me', 'my', 'am', 'not', 'no', 'yes', 'so', 'if', 'then',
    'than', 'too', 'very', 'just', 'also', 'only', 'now', 'here',
    'there', 'when', 'where', 'why', 'how', 'all', 'each', 'every',
    'both', 'few', 'more', 'most', 'other', 'some', 'such', 'any',
    'into', 'through', 'during', 'before', 'after', 'above', 'below',
    'between', 'under', 'again', 'further', 'once', 'up', 'down',
    'out', 'off', 'over', 'about', 'against', 'until', 'while',
    # Event-specific stopwords
    'event', 'events', 'ticket', 'tickets', 'booking', 'bookings',
    'presents', 'present', 'featuring', 'feat', 'ft', 'vs', 'versus',
    'part', 'session', 'sessions', 'day', 'days', 'night', 'nights',
    'week', 'weekend', 'month', 'year', 'annual', 'edition',
    'new', 'live', 'show', 'shows', 'performance', 'performances',
    'pm', 'am', 'gmt', 'bst', 'uk', 'gb', 'united', 'kingdom',
])

# Common venue-related words to filter
VENUE_WORDS = frozenset([
    'hall', 'theatre', 'theater', 'centre', 'center', 'club', 'room',
    'arena', 'stadium', 'park', 'garden', 'gardens', 'house', 'palace',
    'church', 'cathedral', 'chapel', 'school', 'college', 'university',
    'hotel', 'inn', 'pub', 'bar', 'restaurant', 'cafe', 'cinema',
    'gallery', 'museum', 'library', 'studio', 'studios', 'venue',
    'auditorium', 'pavilion', 'marquee', 'tent', 'field', 'ground',
    'street', 'road', 'lane', 'avenue', 'place', 'square', 'way',
])

# Simple stemming rules (suffix stripping)
STEM_RULES = [
    (r'ies$', 'y'),      # parties -> party
    (r'ves$', 'f'),      # wolves -> wolf
    (r'oes$', 'o'),      # heroes -> hero
    (r'ses$', 's'),      # classes -> class
    (r'xes$', 'x'),      # boxes -> box
    (r'ches$', 'ch'),    # matches -> match
    (r'shes$', 'sh'),    # wishes -> wish
    (r'ing$', ''),       # running -> run
    (r'tion$', 't'),     # celebration -> celebrat
    (r'sion$', 's'),     # admission -> admis
    (r'ness$', ''),      # happiness -> happi
    (r'ment$', ''),      # entertainment -> entertain
    (r'able$', ''),      # suitable -> suit
    (r'ible$', ''),      # possible -> poss
    (r'ful$', ''),       # wonderful -> wonder
    (r'less$', ''),      # endless -> end
    (r'ous$', ''),       # famous -> fam
    (r'ive$', ''),       # creative -> creat
    (r'ly$', ''),        # quickly -> quick
    (r'er$', ''),        # runner -> runn
    (r'est$', ''),       # fastest -> fast
    (r'ed$', ''),        # played -> play
    (r's$', ''),         # runs -> run (must be last)
]


def simple_stem(word: str) -> str:
    """
    Apply simple stemming rules to a word.
    This is a lightweight alternative to NLTK's Porter Stemmer.
    """
    if len(word) <= 3:
        return word

    original = word
    for pattern, replacement in STEM_RULES:
        new_word = re.sub(pattern, replacement, word)
        if new_word != word and len(new_word) >= 3:
            word = new_word
            break  # Apply only one rule

    return word


def extract_keywords(event_name: str, min_length: int = 3) -> List[str]:
    """
    Extract meaningful keywords from an event name.

    Args:
        event_name: The event name string
        min_length: Minimum keyword length

    Returns:
        List of cleaned, stemmed keywords
    """
    if not event_name or not isinstance(event_name, str):
        return []

    # Lowercase
    text = event_name.lower()

    # Remove special characters, keep letters and spaces
    text = re.sub(r'[^a-z\s]', ' ', text)

    # Split into words
    words = text.split()

    # Filter and process
    keywords = []
    for word in words:
        # Skip short words
        if len(word) < min_length:
            continue

        # Skip stopwords
        if word in STOPWORDS:
            continue

        # Skip venue words
        if word in VENUE_WORDS:
            continue

        # Skip if it looks like a year (4 digits that could be 19xx or 20xx)
        if re.match(r'^(19|20)\d{2}$', word):
            continue

        # Apply stemming
        stemmed = simple_stem(word)

        # Skip if too short after stemming
        if len(stemmed) < min_length:
            continue

        keywords.append(stemmed)

    return keywords


def build_keyword_frequency_table(booking_df: pd.DataFrame) -> pd.DataFrame:
    """
    Build a frequency table of keywords weighted by count, revenue, and tickets.

    Args:
        booking_df: Booking DataFrame with EventName, PaymentReceived, TicketQuantity

    Returns:
        DataFrame with keyword metrics
    """
    keyword_stats = defaultdict(lambda: {
        'event_count': 0,
        'total_revenue': 0.0,
        'total_tickets': 0,
        'event_ids': set()
    })

    # Process each booking
    for _, row in booking_df.iterrows():
        event_name = row.get('EventName', '')
        event_id = row.get('EventId')
        revenue = row.get('PaymentReceived', 0) or 0
        tickets = row.get('TicketQuantity', 0) or 0

        keywords = extract_keywords(event_name)

        for keyword in keywords:
            stats = keyword_stats[keyword]
            stats['total_revenue'] += revenue
            stats['total_tickets'] += tickets
            if event_id and event_id not in stats['event_ids']:
                stats['event_ids'].add(event_id)
                stats['event_count'] += 1

    # Convert to DataFrame
    rows = []
    for keyword, stats in keyword_stats.items():
        rows.append({
            'Keyword': keyword,
            'Event Count': stats['event_count'],
            'Total Revenue': round(stats['total_revenue'], 2),
            'Total Tickets': stats['total_tickets'],
            'Avg Revenue Per Event': round(stats['total_revenue'] / stats['event_count'], 2) if stats['event_count'] > 0 else 0,
            'Avg Tickets Per Event': round(stats['total_tickets'] / stats['event_count'], 1) if stats['event_count'] > 0 else 0,
        })

    df = pd.DataFrame(rows)
    df = df.sort_values('Total Revenue', ascending=False)

    return df


def analyse_temporal_patterns(booking_df: pd.DataFrame, top_n: int = 100) -> pd.DataFrame:
    """
    Analyse temporal patterns for top keywords by revenue.

    Args:
        booking_df: Booking DataFrame with EventName, EventDate, TransactionDate
        top_n: Number of top keywords to analyse

    Returns:
        DataFrame with temporal pattern analysis
    """
    # First get top keywords by revenue
    keyword_revenue = defaultdict(float)
    keyword_events = defaultdict(set)

    for _, row in booking_df.iterrows():
        event_name = row.get('EventName', '')
        event_id = row.get('EventId')
        revenue = row.get('PaymentReceived', 0) or 0

        for keyword in extract_keywords(event_name):
            keyword_revenue[keyword] += revenue
            if event_id:
                keyword_events[keyword].add(event_id)

    # Get top N keywords
    top_keywords = sorted(keyword_revenue.keys(), key=lambda k: keyword_revenue[k], reverse=True)[:top_n]

    # Build event-level data for analysis
    event_data = {}  # event_id -> {keywords, event_date, first_sale, creation_date, transactions}

    for _, row in booking_df.iterrows():
        event_id = row.get('EventId')
        if not event_id:
            continue

        event_name = row.get('EventName', '')
        event_date = row.get('EventDate')
        transaction_date = row.get('TransactionDate')

        if event_id not in event_data:
            event_data[event_id] = {
                'keywords': set(extract_keywords(event_name)),
                'event_date': event_date,
                'first_sale': transaction_date,
                'last_sale': transaction_date,
                'transactions': []
            }
        else:
            # Update first/last sale dates
            if transaction_date:
                if event_data[event_id]['first_sale'] is None or transaction_date < event_data[event_id]['first_sale']:
                    event_data[event_id]['first_sale'] = transaction_date
                if event_data[event_id]['last_sale'] is None or transaction_date > event_data[event_id]['last_sale']:
                    event_data[event_id]['last_sale'] = transaction_date
            event_data[event_id]['transactions'].append(transaction_date)

    # Analyse each top keyword
    results = []

    for keyword in top_keywords:
        # Get events containing this keyword
        keyword_event_ids = [eid for eid, data in event_data.items() if keyword in data['keywords']]

        if not keyword_event_ids:
            continue

        # Event month distribution
        month_counts = Counter()
        creation_lead_times = []
        sales_lead_times = []

        for eid in keyword_event_ids:
            data = event_data[eid]
            event_date = data['event_date']
            first_sale = data['first_sale']

            if pd.notna(event_date):
                try:
                    event_dt = pd.to_datetime(event_date)
                    month_counts[event_dt.month] += 1

                    # Sales lead time (days before event that first ticket sells)
                    if pd.notna(first_sale):
                        first_sale_dt = pd.to_datetime(first_sale)
                        lead_time = (event_dt - first_sale_dt).days
                        if 0 <= lead_time <= 365:  # Reasonable range
                            sales_lead_times.append(lead_time)
                except:
                    pass

        # Calculate primary months (top 3 by frequency)
        top_months = month_counts.most_common(3)
        primary_months = ', '.join([calendar.month_abbr[m] for m, _ in top_months]) if top_months else 'N/A'

        # Peak month
        peak_month = calendar.month_abbr[top_months[0][0]] if top_months else 'N/A'

        # Average sales lead time
        avg_sales_lead = round(np.mean(sales_lead_times)) if sales_lead_times else None
        median_sales_lead = round(np.median(sales_lead_times)) if sales_lead_times else None

        # Recommended campaign start (based on 75th percentile of sales lead time + 2 weeks buffer)
        if sales_lead_times:
            p75_lead = np.percentile(sales_lead_times, 75)
            recommended_lead = int(p75_lead + 14)  # Add 2 weeks buffer
            recommended_lead_weeks = round(recommended_lead / 7, 1)
        else:
            recommended_lead = None
            recommended_lead_weeks = None

        results.append({
            'Keyword': keyword,
            'Event Count': len(keyword_event_ids),
            'Total Revenue': round(keyword_revenue[keyword], 2),
            'Primary Months': primary_months,
            'Peak Month': peak_month,
            'Avg Sales Lead (days)': avg_sales_lead,
            'Median Sales Lead (days)': median_sales_lead,
            'Recommended Lead (days)': recommended_lead,
            'Recommended Lead (weeks)': recommended_lead_weeks,
        })

    return pd.DataFrame(results)


def find_keyword_associations(booking_df: pd.DataFrame, min_cooccurrence: int = 5) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Find keyword pairs and triplets that frequently appear together.
    Weighted by revenue.

    Args:
        booking_df: Booking DataFrame with EventName, PaymentReceived
        min_cooccurrence: Minimum number of events for a pair/triplet to be included

    Returns:
        Tuple of (pairs_df, triplets_df)
    """
    # Build event-level keyword sets with revenue
    event_keywords = {}  # event_id -> (keywords_set, total_revenue)

    for _, row in booking_df.iterrows():
        event_id = row.get('EventId')
        if not event_id:
            continue

        event_name = row.get('EventName', '')
        revenue = row.get('PaymentReceived', 0) or 0

        keywords = set(extract_keywords(event_name))

        if event_id not in event_keywords:
            event_keywords[event_id] = (keywords, revenue)
        else:
            # Add revenue to existing event
            existing_keywords, existing_revenue = event_keywords[event_id]
            event_keywords[event_id] = (existing_keywords | keywords, existing_revenue + revenue)

    # Count pairs
    pair_stats = defaultdict(lambda: {'count': 0, 'revenue': 0.0})
    triplet_stats = defaultdict(lambda: {'count': 0, 'revenue': 0.0})

    for event_id, (keywords, revenue) in event_keywords.items():
        keyword_list = sorted(keywords)  # Sort for consistent ordering

        # Generate pairs
        for pair in combinations(keyword_list, 2):
            pair_stats[pair]['count'] += 1
            pair_stats[pair]['revenue'] += revenue

        # Generate triplets (only if we have at least 3 keywords)
        if len(keyword_list) >= 3:
            for triplet in combinations(keyword_list, 3):
                triplet_stats[triplet]['count'] += 1
                triplet_stats[triplet]['revenue'] += revenue

    # Filter and sort pairs
    pair_rows = []
    for pair, stats in pair_stats.items():
        if stats['count'] >= min_cooccurrence:
            pair_rows.append({
                'Keyword 1': pair[0],
                'Keyword 2': pair[1],
                'Event Count': stats['count'],
                'Total Revenue': round(stats['revenue'], 2),
                'Avg Revenue': round(stats['revenue'] / stats['count'], 2) if stats['count'] > 0 else 0,
            })

    pairs_df = pd.DataFrame(pair_rows)
    if len(pairs_df) > 0:
        pairs_df = pairs_df.sort_values('Total Revenue', ascending=False)

    # Filter and sort triplets
    triplet_rows = []
    for triplet, stats in triplet_stats.items():
        if stats['count'] >= min_cooccurrence:
            triplet_rows.append({
                'Keyword 1': triplet[0],
                'Keyword 2': triplet[1],
                'Keyword 3': triplet[2],
                'Event Count': stats['count'],
                'Total Revenue': round(stats['revenue'], 2),
                'Avg Revenue': round(stats['revenue'] / stats['count'], 2) if stats['count'] > 0 else 0,
            })

    triplets_df = pd.DataFrame(triplet_rows)
    if len(triplets_df) > 0:
        triplets_df = triplets_df.sort_values('Total Revenue', ascending=False)

    return pairs_df, triplets_df


def get_top_associations_for_keyword(keyword: str, pairs_df: pd.DataFrame, top_n: int = 3) -> str:
    """
    Get the top associated keywords for a given keyword.

    Args:
        keyword: The keyword to find associations for
        pairs_df: DataFrame of keyword pairs
        top_n: Number of top associations to return

    Returns:
        Comma-separated string of top associated keywords
    """
    if len(pairs_df) == 0:
        return ''

    # Find pairs containing this keyword
    mask = (pairs_df['Keyword 1'] == keyword) | (pairs_df['Keyword 2'] == keyword)
    relevant_pairs = pairs_df[mask].head(top_n * 2)  # Get extra in case of duplicates

    associations = []
    for _, row in relevant_pairs.iterrows():
        other = row['Keyword 2'] if row['Keyword 1'] == keyword else row['Keyword 1']
        if other not in associations:
            associations.append(other)
        if len(associations) >= top_n:
            break

    return ', '.join(associations)


def generate_keyword_summary(booking_df: pd.DataFrame, top_n: int = 50) -> pd.DataFrame:
    """
    Generate a comprehensive summary table for top keywords.

    Args:
        booking_df: Booking DataFrame
        top_n: Number of top keywords to include

    Returns:
        Summary DataFrame
    """
    print("  Extracting keywords...")
    freq_table = build_keyword_frequency_table(booking_df)

    print("  Analysing temporal patterns...")
    temporal_df = analyse_temporal_patterns(booking_df, top_n=top_n)

    print("  Finding keyword associations...")
    pairs_df, _ = find_keyword_associations(booking_df)

    # Merge frequency and temporal data
    summary = freq_table.head(top_n).merge(
        temporal_df[['Keyword', 'Primary Months', 'Peak Month', 'Recommended Lead (weeks)']],
        on='Keyword',
        how='left'
    )

    # Add top associations
    summary['Top Associations'] = summary['Keyword'].apply(
        lambda k: get_top_associations_for_keyword(k, pairs_df, top_n=3)
    )

    # Reorder columns
    column_order = [
        'Keyword',
        'Event Count',
        'Total Revenue',
        'Total Tickets',
        'Peak Month',
        'Primary Months',
        'Recommended Lead (weeks)',
        'Top Associations',
        'Avg Revenue Per Event',
        'Avg Tickets Per Event',
    ]

    # Only include columns that exist
    column_order = [c for c in column_order if c in summary.columns]
    summary = summary[column_order]

    return summary


def generate_keyword_analysis_csvs(booking_df: pd.DataFrame, output_file: str) -> Dict[str, str]:
    """
    Generate all keyword analysis CSV files.

    Args:
        booking_df: Booking DataFrame
        output_file: Base output filename

    Returns:
        Dictionary mapping report name to file path
    """
    base_name = output_file.rsplit('.', 1)[0]
    output_files = {}

    print("\nGenerating keyword analysis reports...")

    # 1. Full keyword frequency table
    print("  Building keyword frequency table...")
    freq_table = build_keyword_frequency_table(booking_df)
    freq_file = f"{base_name}_keywords_frequency.csv"
    freq_table.to_csv(freq_file, index=False, float_format='%.2f')
    output_files['frequency'] = freq_file
    print(f"    Saved: {freq_file} ({len(freq_table)} keywords)")

    # 2. Temporal patterns for top 100
    print("  Analysing temporal patterns...")
    temporal_df = analyse_temporal_patterns(booking_df, top_n=100)
    temporal_file = f"{base_name}_keywords_temporal.csv"
    temporal_df.to_csv(temporal_file, index=False, float_format='%.2f')
    output_files['temporal'] = temporal_file
    print(f"    Saved: {temporal_file}")

    # 3. Keyword associations (pairs and triplets)
    print("  Finding keyword associations...")
    pairs_df, triplets_df = find_keyword_associations(booking_df)

    pairs_file = f"{base_name}_keywords_pairs.csv"
    pairs_df.head(500).to_csv(pairs_file, index=False, float_format='%.2f')
    output_files['pairs'] = pairs_file
    print(f"    Saved: {pairs_file} ({len(pairs_df)} pairs)")

    triplets_file = f"{base_name}_keywords_triplets.csv"
    triplets_df.head(200).to_csv(triplets_file, index=False, float_format='%.2f')
    output_files['triplets'] = triplets_file
    print(f"    Saved: {triplets_file} ({len(triplets_df)} triplets)")

    # 4. Summary table (top 50)
    print("  Generating summary table...")
    summary_df = generate_keyword_summary(booking_df, top_n=50)
    summary_file = f"{base_name}_keywords_summary.csv"
    summary_df.to_csv(summary_file, index=False, float_format='%.2f')
    output_files['summary'] = summary_file
    print(f"    Saved: {summary_file}")

    return output_files
