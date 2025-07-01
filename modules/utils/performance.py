"""
Performance optimization utilities for TryBooking reports.
"""
import pandas as pd
import functools
import time
from typing import Dict, Any


def optimize_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Optimize DataFrame data types to reduce memory usage.
    
    Args:
        df: DataFrame to optimize
    
    Returns:
        Optimized DataFrame
    """
    # Convert string columns to category if they have low cardinality
    for col in df.select_dtypes(include=['object']).columns:
        if df[col].nunique() / len(df) < 0.5:  # Less than 50% unique values
            df[col] = df[col].astype('category')
    
    # Downcast numeric columns
    for col in df.select_dtypes(include=['int']).columns:
        df[col] = pd.to_numeric(df[col], downcast='integer')
    
    for col in df.select_dtypes(include=['float']).columns:
        df[col] = pd.to_numeric(df[col], downcast='float')
    
    return df


def chunk_dataframe(df: pd.DataFrame, chunk_size: int = 10000):
    """
    Split DataFrame into chunks for processing large datasets.
    
    Args:
        df: DataFrame to chunk
        chunk_size: Size of each chunk
    
    Yields:
        DataFrame chunks
    """
    for start in range(0, len(df), chunk_size):
        yield df.iloc[start:start + chunk_size]


def timer_decorator(func):
    """
    Decorator to time function execution.
    
    Usage:
        @timer_decorator
        def my_function():
            pass
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        print(f"{func.__name__} took {end_time - start_time:.2f} seconds")
        return result
    return wrapper


def batch_process(items: list, batch_size: int = 100, process_func=None, desc: str = "Processing"):
    """
    Process items in batches with progress reporting.
    
    Args:
        items: List of items to process
        batch_size: Size of each batch
        process_func: Function to apply to each batch
        desc: Description for progress messages
    
    Returns:
        List of results from all batches
    """
    results = []
    total_batches = (len(items) + batch_size - 1) // batch_size
    
    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        batch_num = (i // batch_size) + 1
        
        print(f"{desc}: Batch {batch_num}/{total_batches} ({len(batch)} items)")
        
        if process_func:
            batch_result = process_func(batch)
            results.extend(batch_result if isinstance(batch_result, list) else [batch_result])
        else:
            results.extend(batch)
    
    return results


def cache_result(cache_dict: Dict[str, Any]):
    """
    Simple in-memory cache decorator.
    
    Args:
        cache_dict: Dictionary to use for caching
    
    Usage:
        cache = {}
        
        @cache_result(cache)
        def expensive_function(param):
            return compute_something(param)
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Create a cache key from function name and arguments
            cache_key = f"{func.__name__}:{str(args)}:{str(kwargs)}"
            
            if cache_key in cache_dict:
                return cache_dict[cache_key]
            
            result = func(*args, **kwargs)
            cache_dict[cache_key] = result
            return result
        return wrapper
    return decorator