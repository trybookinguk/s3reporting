"""
Event Keyword and Timing Analysis Module

Extracts marketing intelligence from event data:
- Keyword extraction from event names
- Temporal patterns (when events happen, lead times)
- Keyword associations (co-occurring terms)

Optimised for large datasets using vectorised operations.
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
    (re.compile(r'ies$'), 'y'),      # parties -> party
    (re.compile(r'ves$'), 'f'),      # wolves -> wolf
    (re.compile(r'oes$'), 'o'),      # heroes -> hero
    (re.compile(r'ses$'), 's'),      # classes -> class
    (re.compile(r'xes$'), 'x'),      # boxes -> box
    (re.compile(r'ches$'), 'ch'),    # matches -> match
    (re.compile(r'shes$'), 'sh'),    # wishes -> wish
    (re.compile(r'ing$'), ''),       # running -> run
    (re.compile(r'tion$'), 't'),     # celebration -> celebrat
    (re.compile(r'sion$'), 's'),     # admission -> admis
    (re.compile(r'ness$'), ''),      # happiness -> happi
    (re.compile(r'ment$'), ''),      # entertainment -> entertain
    (re.compile(r'able$'), ''),      # suitable -> suit
    (re.compile(r'ible$'), ''),      # possible -> poss
    (re.compile(r'ful$'), ''),       # wonderful -> wonder
    (re.compile(r'less$'), ''),      # endless -> end
    (re.compile(r'ous$'), ''),       # famous -> fam
    (re.compile(r'ive$'), ''),       # creative -> creat
    (re.compile(r'ly$'), ''),        # quickly -> quick
    (re.compile(r'er$'), ''),        # runner -> runn
    (re.compile(r'est$'), ''),       # fastest -> fast
    (re.compile(r'ed$'), ''),        # played -> play
    (re.compile(r's$'), ''),         # runs -> run (must be last)
]

# Words that should NOT be stemmed (false positives from suffix rules)
STEM_EXCEPTIONS = frozenset([
    # Seasons and months
    'summer', 'winter', 'spring', 'autumn', 'october', 'november', 'december',
    'september', 'january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
    # Holidays and events
    'easter', 'christmas', 'halloween', 'festival', 'carnival', 'concert',
    'theater', 'theatre', 'dinner', 'supper', 'brunch', 'lunch', 'breakfast',
    # Family/people words
    'master', 'sister', 'brother', 'mother', 'father', 'daughter', 'member',
    'singer', 'dancer', 'performer', 'player', 'speaker', 'teacher', 'leader',
    'runner', 'walker', 'swimmer', 'rider', 'driver', 'trainer', 'instructor',
    # Common nouns that shouldn't be stemmed
    'corner', 'river', 'water', 'order', 'border', 'wonder', 'number',
    'rubber', 'butter', 'letter', 'matter', 'better', 'bitter', 'litter',
    'glitter', 'twitter', 'silver', 'copper', 'super', 'clever', 'never',
    'ever', 'over', 'under', 'proper', 'paper', 'power', 'tower', 'flower',
    'shower', 'lower', 'murder', 'thunder', 'gender', 'tender', 'slender',
    # Event-specific words
    'beer', 'cider', 'wine', 'jazz', 'blues', 'rock', 'folk', 'opera',
    'choir', 'chorus', 'orchestra', 'band', 'disco', 'karaoke', 'bingo',
    'poker', 'snooker', 'darts', 'cricket', 'football', 'rugby', 'tennis',
    'netball', 'hockey', 'roller', 'skater', 'yoga', 'pilates', 'zumba',
    'boxing', 'wrestling', 'fencing', 'archery', 'pottery', 'painting',
    'knitting', 'sewing', 'quilting', 'crafts', 'maker', 'farmer', 'gardener',
    'murder', 'mystery', 'thriller', 'horror', 'comedy', 'drama', 'pantomime',
    'puppet', 'magic', 'circus', 'acrobat', 'juggler', 'clown', 'storyteller',
    'whisky', 'whiskey', 'gin', 'cocktail', 'prosecco', 'champagne',
    # Time-related words
    'morning', 'afternoon', 'evening', 'midnight', 'sunrise', 'sunset',
    'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday',
    'weekly', 'monthly', 'daily', 'nightly', 'yearly', 'annual',
])

# Synonym groups - map variations to canonical form
# Keys are the canonical form, values are variations that should map to it
SYNONYMS = {
    'christmas': ['xmas', 'x-mas', 'festive'],
    'children': ['kids', 'kidz', 'child', 'junior', 'juniors'],
    'barbecue': ['bbq', 'barbeque'],
    'versus': ['vs', 'v'],
    'and': ['&'],
    'quiz': ['quizz', 'quizes'],
    '5k': ['5km', 'five k', 'fivek'],
    '10k': ['10km', 'ten k', 'tenk'],
    'half marathon': ['half-marathon', 'halfmarathon'],
    'parkrun': ['park run', 'park-run'],
    'open mic': ['open-mic', 'openmic'],
    'st': ['saint'],
    'dj': ['deejay', 'd.j.'],
    'acoustic': ['unplugged'],
    'tribute': ['tribute band', 'tribute act'],
    'family': ['families'],
    'parent': ['parents', 'mum', 'mums', 'dad', 'dads'],
    'senior': ['seniors', 'oap', 'oaps', 'pensioner', 'pensioners', 'over 60', 'over 65'],
    'beginners': ['beginner', 'beginers', 'newbie', 'newbies', 'intro', 'introduction'],
    'advanced': ['intermediate', 'improvers'],
    'workshop': ['masterclass', 'master class', 'class'],
    'fair': ['fayre', 'fete', 'fête'],
    'market': ['markets'],
    'ball': ['gala', 'prom'],
    'race': ['racing'],
    'walk': ['walking', 'hike', 'hiking', 'ramble', 'rambling'],
    'run': ['running', 'jog', 'jogging'],
    'swim': ['swimming'],
    'cycle': ['cycling', 'bike', 'biking'],
    'dance': ['dancing'],
    'sing': ['singing'],
    'paint': ['painting'],
    'craft': ['crafts', 'crafting'],
    'cook': ['cooking', 'cookery'],
    'bake': ['baking'],
    'taste': ['tasting'],
    'tour': ['tours', 'touring'],
    'talk': ['talks', 'lecture', 'lectures'],
    'screening': ['film', 'movie', 'cinema'],
    'exhibition': ['exhibit', 'expo'],
}

# Build reverse lookup for synonyms
_SYNONYM_LOOKUP = {}
for canonical, variations in SYNONYMS.items():
    for var in variations:
        _SYNONYM_LOOKUP[var.lower()] = canonical.lower()

# Multi-word phrases to detect (in order of specificity - longer phrases first)
# These will be extracted as single keywords
MULTI_WORD_PHRASES = [
    # 3+ word phrases
    'car boot sale', 'nearly new sale', 'table top sale', 'jumble sale',
    'craft and design', 'arts and crafts', 'food and drink',
    'murder mystery dinner', 'murder mystery evening', 'murder mystery night',
    'wine and cheese', 'cheese and wine', 'gin and tonic',
    'mums and babies', 'parent and child', 'parent and toddler',
    'mother and baby', 'father and son', 'mother and daughter',
    'live music night', 'open mic night', 'quiz night', 'curry night',
    'fish and chips', 'pie and mash', 'sunday roast', 'afternoon tea',
    'new years eve', 'bonfire night', 'guy fawkes', 'burns night',
    'st patricks day', 'st valentines', 'mothers day', 'fathers day',
    'easter egg hunt', 'easter bunny', 'santa grotto', 'meet santa',
    'spring fair', 'summer fair', 'autumn fair', 'winter fair',
    'christmas fair', 'christmas market', 'christmas party',
    'halloween party', 'fancy dress', 'black tie',
    'fun run', 'charity run', 'santa run', 'colour run', 'color run',
    'park run', 'mud run', 'obstacle course', 'tough mudder',
    'half marathon', 'full marathon',
    'pub quiz', 'table quiz', 'charity quiz',
    'speed dating', 'singles night',
    'drag queen', 'drag brunch', 'drag bingo',
    'silent disco', 'silent walk',
    'coffee morning', 'coffee and cake',
    'book club', 'film club', 'supper club', 'lunch club',
    'baby sensory', 'baby massage', 'baby yoga', 'toddler group',
    'play group', 'stay and play',
    'nature walk', 'woodland walk', 'coast path',
    'ghost walk', 'ghost tour', 'ghost hunt',
    'escape room', 'treasure hunt',
    'dog show', 'dog walk', 'cat show',
    'vintage fair', 'antiques fair', 'record fair', 'book fair',
    'jobs fair', 'careers fair', 'wedding fair', 'bridal fair',
    'beer festival', 'cider festival', 'wine festival', 'gin festival',
    'food festival', 'music festival', 'comedy festival', 'arts festival',
    'folk festival', 'jazz festival', 'blues festival',
    'village fete', 'church fete', 'school fete', 'summer fete',
    'street party', 'garden party', 'tea party', 'pool party',
    'hen party', 'stag party', 'birthday party', 'kids party',
    'leaving party', 'retirement party', 'office party',
    'prize draw', 'prize giving', 'awards ceremony',
    'annual general meeting', 'agm',
    'open day', 'open evening', 'open house', 'open garden',
    'taster session', 'taster day', 'trial class',
    'drop in', 'pop up',
    'boot camp', 'fitness class', 'exercise class',
    'spin class', 'yoga class', 'pilates class', 'zumba class',
    'art class', 'painting class', 'drawing class', 'pottery class',
    'dance class', 'ballet class', 'tap class', 'salsa class',
    'language class', 'french class', 'spanish class',
    'first aid', 'life saving',
    'driving test', 'theory test',
    'blood donor', 'blood donation',
    # 2 word phrases
    'craft fair', 'christmas fayre', 'summer fayre', 'winter fayre',
    'car boot', 'boot sale', 'table top', 'jumble sale',
    'farmers market', 'artisan market', 'night market', 'street market',
    'flea market', 'vintage market', 'makers market',
    'open mic', 'live music', 'tribute night', 'karaoke night',
    'quiz night', 'curry night', 'steak night', 'pizza night',
    'comedy night', 'casino night', 'race night', 'bingo night',
    'murder mystery', 'escape room', 'treasure hunt',
    'ghost tour', 'ghost walk', 'walking tour', 'bus tour',
    'wine tasting', 'beer tasting', 'gin tasting', 'whisky tasting',
    'food tour', 'pub crawl',
    'speed dating', 'singles event',
    'networking event', 'business breakfast',
    'coffee morning', 'afternoon tea', 'high tea', 'cream tea',
    'sunday lunch', 'sunday roast', 'sunday brunch',
    'drag show', 'cabaret show', 'variety show', 'magic show',
    'puppet show', 'circus skills', 'face painting',
    'craft workshop', 'art workshop', 'writing workshop',
    'yoga retreat', 'wellness retreat', 'spa day',
    'hen do', 'stag do', 'baby shower', 'gender reveal',
    'fun day', 'family day', 'sports day', 'activity day',
    'charity event', 'fundraising event', 'charity ball',
    'awards night', 'gala dinner', 'black tie',
    'fancy dress', 'costume party',
    'new year', 'nye', 'hogmanay',
    'bonfire', 'fireworks', 'firework display',
    'easter egg', 'egg hunt',
    'half term', 'school holidays', 'bank holiday',
    'teddy bear', 'teddy bears',
    'car show', 'bike show', 'motor show',
    'dog walk', 'dog training', 'puppy class',
    'nature reserve', 'bird watching', 'star gazing',
    'beach clean', 'litter pick',
    'park run', 'fun run', 'colour run',
    'swimming gala', 'sports tournament',
    'football tournament', 'cricket match', 'rugby match',
    'golf day', 'tennis tournament',
    'panto', 'pantomime',
    'nativity', 'carol service', 'carols', 'christingle',
    'remembrance', 'memorial',
    'wedding fayre', 'bridal show',
]

# Pre-compile phrase patterns for efficiency
_PHRASE_PATTERNS = [(phrase, re.compile(r'\b' + re.escape(phrase) + r'\b', re.IGNORECASE))
                    for phrase in sorted(MULTI_WORD_PHRASES, key=len, reverse=True)]

# Pre-compiled regex for cleaning
YEAR_PATTERN = re.compile(r'^(19|20)\d{2}$')
NON_ALPHA_PATTERN = re.compile(r'[^a-z\s]')


def simple_stem(word: str) -> str:
    """
    Apply simple stemming rules to a word.
    This is a lightweight alternative to NLTK's Porter Stemmer.
    """
    if len(word) <= 3:
        return word

    # Don't stem words in the exceptions list
    if word in STEM_EXCEPTIONS:
        return word

    for pattern, replacement in STEM_RULES:
        new_word = pattern.sub(replacement, word)
        if new_word != word and len(new_word) >= 3:
            return new_word

    return word


def normalise_synonym(word: str) -> str:
    """
    Normalise a word to its canonical form if it's a known synonym.

    Args:
        word: The word to normalise

    Returns:
        Canonical form if synonym found, otherwise original word
    """
    return _SYNONYM_LOOKUP.get(word.lower(), word)


def extract_phrases(text: str) -> Tuple[List[str], str]:
    """
    Extract multi-word phrases from text, returning phrases found and remaining text.

    Args:
        text: The text to extract phrases from

    Returns:
        Tuple of (list of phrases found, remaining text with phrases removed)
    """
    phrases_found = []
    remaining = text.lower()

    for phrase, pattern in _PHRASE_PATTERNS:
        match = pattern.search(remaining)
        if match:
            # Use underscore-joined version as the keyword
            phrase_keyword = phrase.replace(' ', '_')
            phrases_found.append(phrase_keyword)
            # Remove the phrase from remaining text to avoid double-counting
            remaining = pattern.sub(' ', remaining)

    return phrases_found, remaining


def extract_keywords(event_name: str, min_length: int = 3) -> List[str]:
    """
    Extract meaningful keywords from an event name.

    Includes:
    - Multi-word phrase detection (e.g., "craft fair" → "craft_fair")
    - Synonym normalisation (e.g., "xmas" → "christmas")
    - Simple stemming with exceptions

    Args:
        event_name: The event name string
        min_length: Minimum keyword length

    Returns:
        List of cleaned, normalised keywords
    """
    if not event_name or not isinstance(event_name, str):
        return []

    # Step 1: Extract multi-word phrases first
    phrases, remaining_text = extract_phrases(event_name)

    # Step 2: Clean remaining text
    text = NON_ALPHA_PATTERN.sub(' ', remaining_text.lower())

    # Step 3: Process individual words
    keywords = list(phrases)  # Start with phrases found

    for word in text.split():
        # Skip short words, stopwords, venue words, years
        if (len(word) >= min_length and
            word not in STOPWORDS and
            word not in VENUE_WORDS and
            not YEAR_PATTERN.match(word)):

            # Apply synonym normalisation
            normalised = normalise_synonym(word)

            # Apply stemming
            stemmed = simple_stem(normalised)

            if len(stemmed) >= min_length:
                keywords.append(stemmed)

    return keywords


def _aggregate_events(booking_df: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate booking data to event level for efficient processing.

    Returns DataFrame with one row per event containing:
    - EventId, EventName, total_revenue, total_tickets, total_fees, event_date, first_sale
    """
    # Ensure required columns exist
    required_cols = ['EventId', 'EventName']
    if not all(col in booking_df.columns for col in required_cols):
        return pd.DataFrame()

    # Calculate fees if fee columns exist
    fee_columns = ['BookingFee', 'CardFee', 'ProcessingFee', 'TicketFee']
    has_fees = all(col in booking_df.columns for col in fee_columns)

    if has_fees:
        booking_df = booking_df.copy()
        booking_df['_total_fees'] = (
            booking_df['BookingFee'].fillna(0) +
            booking_df['CardFee'].fillna(0) +
            booking_df['ProcessingFee'].fillna(0) +
            booking_df['TicketFee'].fillna(0)
        )

    # Aggregate to event level
    agg_dict = {
        'EventName': 'first',
        'PaymentReceived': 'sum',
        'TicketQuantity': 'sum',
    }

    if has_fees:
        agg_dict['_total_fees'] = 'sum'

    if 'EventDate' in booking_df.columns:
        agg_dict['EventDate'] = 'first'
    if 'TransactionDate' in booking_df.columns:
        agg_dict['TransactionDate'] = 'min'  # First sale

    events_df = booking_df.groupby('EventId').agg(agg_dict).reset_index()

    # Rename columns
    rename_dict = {
        'PaymentReceived': 'total_revenue',
        'TicketQuantity': 'total_tickets',
        'TransactionDate': 'first_sale'
    }
    if has_fees:
        rename_dict['_total_fees'] = 'total_fees'

    events_df = events_df.rename(columns=rename_dict)

    return events_df


def build_keyword_frequency_table(booking_df: pd.DataFrame) -> pd.DataFrame:
    """
    Build a frequency table of keywords weighted by count, revenue, fees, and tickets.
    Optimised to aggregate at event level first.

    Args:
        booking_df: Booking DataFrame with EventName, PaymentReceived, TicketQuantity, fee columns

    Returns:
        DataFrame with keyword metrics including fees
    """
    # Aggregate to event level first
    events_df = _aggregate_events(booking_df)

    if len(events_df) == 0:
        return pd.DataFrame()

    # Extract keywords for each event (vectorised apply is faster than iterrows)
    events_df['keywords'] = events_df['EventName'].apply(extract_keywords)

    # Build keyword stats
    keyword_stats = defaultdict(lambda: {'event_count': 0, 'total_revenue': 0.0, 'total_tickets': 0, 'total_fees': 0.0})

    for _, row in events_df.iterrows():
        keywords = row['keywords']
        revenue = row.get('total_revenue', 0) or 0
        tickets = row.get('total_tickets', 0) or 0
        fees = row.get('total_fees', 0) or 0

        for keyword in keywords:
            stats = keyword_stats[keyword]
            stats['event_count'] += 1
            stats['total_revenue'] += revenue
            stats['total_tickets'] += tickets
            stats['total_fees'] += fees

    # Convert to DataFrame
    rows = []
    for keyword, stats in keyword_stats.items():
        event_count = stats['event_count']
        row_data = {
            'Keyword': keyword,
            'Event Count': event_count,
            'Total Fees': round(stats['total_fees'], 2),
            'Total Revenue': round(stats['total_revenue'], 2),
            'Total Tickets': stats['total_tickets'],
            'Avg Fees Per Event': round(stats['total_fees'] / event_count, 2) if event_count > 0 else 0,
            'Avg Revenue Per Event': round(stats['total_revenue'] / event_count, 2) if event_count > 0 else 0,
            'Avg Tickets Per Event': round(stats['total_tickets'] / event_count, 1) if event_count > 0 else 0,
        }
        rows.append(row_data)

    df = pd.DataFrame(rows)
    if len(df) > 0:
        # Sort by fees (TryBooking's revenue) by default
        df = df.sort_values('Total Fees', ascending=False)

    return df


def analyse_temporal_patterns(booking_df: pd.DataFrame, top_n: int = 100) -> pd.DataFrame:
    """
    Analyse temporal patterns for top keywords by fees.
    Optimised to work at event level.

    Args:
        booking_df: Booking DataFrame with EventName, EventDate, TransactionDate
        top_n: Number of top keywords to analyse

    Returns:
        DataFrame with temporal pattern analysis
    """
    # Aggregate to event level
    events_df = _aggregate_events(booking_df)

    if len(events_df) == 0:
        return pd.DataFrame()

    # Check if fees are available
    has_fees = 'total_fees' in events_df.columns

    # Extract keywords
    events_df['keywords'] = events_df['EventName'].apply(extract_keywords)

    # Build keyword fee and revenue totals first
    keyword_fees = defaultdict(float)
    keyword_revenue = defaultdict(float)
    for _, row in events_df.iterrows():
        fees = row.get('total_fees', 0) or 0
        revenue = row.get('total_revenue', 0) or 0
        for keyword in row['keywords']:
            keyword_fees[keyword] += fees
            keyword_revenue[keyword] += revenue

    # Get top N keywords by fees (or revenue if fees not available)
    sort_dict = keyword_fees if has_fees else keyword_revenue
    top_keywords = sorted(sort_dict.keys(), key=lambda k: sort_dict[k], reverse=True)[:top_n]

    # Prepare event date and first sale columns
    has_event_date = 'EventDate' in events_df.columns
    has_first_sale = 'first_sale' in events_df.columns

    if has_event_date:
        events_df['event_month'] = pd.to_datetime(events_df['EventDate'], errors='coerce').dt.month

    if has_event_date and has_first_sale:
        event_dates = pd.to_datetime(events_df['EventDate'], errors='coerce')
        first_sales = pd.to_datetime(events_df['first_sale'], errors='coerce')
        events_df['sales_lead_days'] = (event_dates - first_sales).dt.days

    # Analyse each top keyword
    results = []

    for keyword in top_keywords:
        # Get events containing this keyword
        mask = events_df['keywords'].apply(lambda kws: keyword in kws)
        keyword_events = events_df[mask]

        if len(keyword_events) == 0:
            continue

        event_count = len(keyword_events)

        # Event month distribution
        if has_event_date and 'event_month' in keyword_events.columns:
            month_counts = keyword_events['event_month'].dropna().astype(int).value_counts()
            top_months = month_counts.head(3)
            primary_months = ', '.join([calendar.month_abbr[m] for m in top_months.index]) if len(top_months) > 0 else 'N/A'
            peak_month = calendar.month_abbr[top_months.index[0]] if len(top_months) > 0 else 'N/A'
        else:
            primary_months = 'N/A'
            peak_month = 'N/A'

        # Sales lead time
        if 'sales_lead_days' in keyword_events.columns:
            valid_leads = keyword_events['sales_lead_days'].dropna()
            valid_leads = valid_leads[(valid_leads >= 0) & (valid_leads <= 365)]

            if len(valid_leads) > 0:
                avg_sales_lead = round(valid_leads.mean())
                median_sales_lead = round(valid_leads.median())
                p75_lead = valid_leads.quantile(0.75)
                recommended_lead = int(p75_lead + 14)  # Add 2 weeks buffer
                recommended_lead_weeks = round(recommended_lead / 7, 1)
            else:
                avg_sales_lead = None
                median_sales_lead = None
                recommended_lead = None
                recommended_lead_weeks = None
        else:
            avg_sales_lead = None
            median_sales_lead = None
            recommended_lead = None
            recommended_lead_weeks = None

        results.append({
            'Keyword': keyword,
            'Event Count': event_count,
            'Total Fees': round(keyword_fees[keyword], 2),
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
    Weighted by fees. Optimised for performance.

    Args:
        booking_df: Booking DataFrame with EventName, fee columns
        min_cooccurrence: Minimum number of events for a pair/triplet to be included

    Returns:
        Tuple of (pairs_df, triplets_df)
    """
    # Aggregate to event level
    events_df = _aggregate_events(booking_df)

    if len(events_df) == 0:
        return pd.DataFrame(), pd.DataFrame()

    # Extract keywords
    events_df['keywords'] = events_df['EventName'].apply(lambda x: tuple(sorted(set(extract_keywords(x)))))

    # Count pairs and triplets
    pair_stats = defaultdict(lambda: {'count': 0, 'fees': 0.0, 'revenue': 0.0})
    triplet_stats = defaultdict(lambda: {'count': 0, 'fees': 0.0, 'revenue': 0.0})

    for _, row in events_df.iterrows():
        keywords = row['keywords']
        fees = row.get('total_fees', 0) or 0
        revenue = row.get('total_revenue', 0) or 0

        if len(keywords) >= 2:
            for pair in combinations(keywords, 2):
                pair_stats[pair]['count'] += 1
                pair_stats[pair]['fees'] += fees
                pair_stats[pair]['revenue'] += revenue

        if len(keywords) >= 3:
            for triplet in combinations(keywords, 3):
                triplet_stats[triplet]['count'] += 1
                triplet_stats[triplet]['fees'] += fees
                triplet_stats[triplet]['revenue'] += revenue

    # Filter and sort pairs
    pair_rows = []
    for pair, stats in pair_stats.items():
        if stats['count'] >= min_cooccurrence:
            pair_rows.append({
                'Keyword 1': pair[0],
                'Keyword 2': pair[1],
                'Event Count': stats['count'],
                'Total Fees': round(stats['fees'], 2),
                'Total Revenue': round(stats['revenue'], 2),
                'Avg Fees': round(stats['fees'] / stats['count'], 2) if stats['count'] > 0 else 0,
            })

    pairs_df = pd.DataFrame(pair_rows)
    if len(pairs_df) > 0:
        pairs_df = pairs_df.sort_values('Total Fees', ascending=False)

    # Filter and sort triplets
    triplet_rows = []
    for triplet, stats in triplet_stats.items():
        if stats['count'] >= min_cooccurrence:
            triplet_rows.append({
                'Keyword 1': triplet[0],
                'Keyword 2': triplet[1],
                'Keyword 3': triplet[2],
                'Event Count': stats['count'],
                'Total Fees': round(stats['fees'], 2),
                'Total Revenue': round(stats['revenue'], 2),
                'Avg Fees': round(stats['fees'] / stats['count'], 2) if stats['count'] > 0 else 0,
            })

    triplets_df = pd.DataFrame(triplet_rows)
    if len(triplets_df) > 0:
        triplets_df = triplets_df.sort_values('Total Fees', ascending=False)

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
    relevant_pairs = pairs_df[mask].head(top_n * 2)

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

    if len(freq_table) == 0:
        return pd.DataFrame()

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

    # Reorder columns - fees first as that's TryBooking's revenue
    column_order = [
        'Keyword',
        'Event Count',
        'Total Fees',
        'Total Revenue',
        'Total Tickets',
        'Peak Month',
        'Primary Months',
        'Recommended Lead (weeks)',
        'Top Associations',
        'Avg Fees Per Event',
        'Avg Revenue Per Event',
        'Avg Tickets Per Event',
    ]

    # Only include columns that exist
    column_order = [c for c in column_order if c in summary.columns]
    summary = summary[column_order]

    return summary


def generate_keyword_analysis_csvs(booking_df: pd.DataFrame, output_file: str, output_folder: str = None) -> Dict[str, str]:
    """
    Generate all keyword analysis CSV files.

    Args:
        booking_df: Booking DataFrame
        output_file: Base output filename
        output_folder: Optional folder to save files in (will be created if needed)

    Returns:
        Dictionary mapping report name to file path
    """
    import os

    base_name = output_file.rsplit('.', 1)[0]
    base_dir = os.path.dirname(base_name) or '.'
    base_file = os.path.basename(base_name)

    # Use output folder if specified
    if output_folder:
        folder_path = os.path.join(base_dir, output_folder)
        os.makedirs(folder_path, exist_ok=True)
        base_name = os.path.join(folder_path, base_file)

    output_files = {}

    print("\nGenerating keyword analysis reports...")

    # Check if we have required columns
    if 'EventName' not in booking_df.columns or 'EventId' not in booking_df.columns:
        print("  Warning: Missing EventName or EventId columns, skipping keyword analysis")
        return output_files

    # 1. Full keyword frequency table
    print("  Building keyword frequency table...")
    freq_table = build_keyword_frequency_table(booking_df)
    if len(freq_table) > 0:
        freq_file = f"{base_name}_keywords_frequency.csv"
        freq_table.to_csv(freq_file, index=False, float_format='%.2f')
        output_files['frequency'] = freq_file
        print(f"    Saved: {freq_file} ({len(freq_table)} keywords)")
    else:
        print("    Warning: No keywords extracted")
        return output_files

    # 2. Temporal patterns for top 100
    print("  Analysing temporal patterns...")
    temporal_df = analyse_temporal_patterns(booking_df, top_n=100)
    if len(temporal_df) > 0:
        temporal_file = f"{base_name}_keywords_temporal.csv"
        temporal_df.to_csv(temporal_file, index=False, float_format='%.2f')
        output_files['temporal'] = temporal_file
        print(f"    Saved: {temporal_file}")

    # 3. Keyword associations (pairs and triplets)
    print("  Finding keyword associations...")
    pairs_df, triplets_df = find_keyword_associations(booking_df)

    if len(pairs_df) > 0:
        pairs_file = f"{base_name}_keywords_pairs.csv"
        pairs_df.head(500).to_csv(pairs_file, index=False, float_format='%.2f')
        output_files['pairs'] = pairs_file
        print(f"    Saved: {pairs_file} ({len(pairs_df)} pairs)")

    if len(triplets_df) > 0:
        triplets_file = f"{base_name}_keywords_triplets.csv"
        triplets_df.head(200).to_csv(triplets_file, index=False, float_format='%.2f')
        output_files['triplets'] = triplets_file
        print(f"    Saved: {triplets_file} ({len(triplets_df)} triplets)")

    # 4. Summary table (top 50)
    print("  Generating summary table...")
    summary_df = generate_keyword_summary(booking_df, top_n=50)
    if len(summary_df) > 0:
        summary_file = f"{base_name}_keywords_summary.csv"
        summary_df.to_csv(summary_file, index=False, float_format='%.2f')
        output_files['summary'] = summary_file
        print(f"    Saved: {summary_file}")

    return output_files
