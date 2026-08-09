from rotor.gateway.accounting import AccountingService


def test_extract_usage_anthropic_top_level_cache_fields():
    """extract_usage recognizes Anthropic's top-level cache_read_input_tokens."""
    svc = AccountingService()
    data = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 300,
        }
    }
    result = svc.extract_usage(data)
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 50
    assert result.cached_tokens == 300


def test_extract_usage_openai_nested_cached_tokens_still_works():
    """OpenAI's prompt_tokens_details.cached_tokens path must continue to work."""
    svc = AccountingService()
    data = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 80},
        }
    }
    result = svc.extract_usage(data)
    assert result.cached_tokens == 80


def test_extract_usage_anthropic_in_details_fallback():
    """Some conversion paths put cached_tokens inside input_tokens_details."""
    svc = AccountingService()
    data = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "input_tokens_details": {"cached_tokens": 40},
        }
    }
    result = svc.extract_usage(data)
    assert result.cached_tokens == 40


def test_streaming_usage_passes_cached_tokens():
    """streaming_usage threads cached_tokens through."""
    svc = AccountingService()
    result = svc.streaming_usage(
        prompt_tokens=100,
        completion_tokens=50,
        has_provider_usage=True,
        cached_tokens=60,
    )
    assert result.cached_tokens == 60
    assert result.prompt_tokens == 100


def test_streaming_usage_no_provider_usage_returns_missing():
    svc = AccountingService()
    result = svc.streaming_usage(
        prompt_tokens=0,
        completion_tokens=0,
        has_provider_usage=False,
        cached_tokens=60,
    )
    assert result.usage_source == "missing"
    assert result.cached_tokens == 0
