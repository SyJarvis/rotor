from rotor.api.v1.responses import ResponsesStreamTransform, chat_response_to_response


def test_chat_response_converts_to_responses_shape() -> None:
    response = chat_response_to_response({
        "id": "chatcmpl_1",
        "created": 123,
        "model": "test-model",
        "choices": [{
            "message": {"role": "assistant", "content": "hello"},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 4,
            "completion_tokens": 2,
            "total_tokens": 6,
        },
    })

    assert response["object"] == "response"
    assert response["output"][0]["content"][0]["text"] == "hello"
    assert response["usage"]["input_tokens"] == 4
    assert response["usage"]["output_tokens"] == 2
    assert response["usage"]["total_tokens"] == 6


def test_chat_stream_converts_to_responses_event_sequence() -> None:
    transform = ResponsesStreamTransform("test-model")

    events = transform.start()
    events += transform.feed({
        "choices": [{
            "delta": {"content": "hel"},
            "finish_reason": None,
        }],
    })
    events += transform.feed({
        "choices": [{
            "delta": {"content": "lo"},
            "finish_reason": None,
        }],
    })
    events += transform.finish({
        "input_tokens": 4,
        "output_tokens": 2,
        "total_tokens": 6,
    })

    event_types = [event["type"] for event in events]
    assert event_types == [
        "response.created",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert events[-1]["response"]["output"][0]["content"][0]["text"] == "hello"
    assert events[-1]["response"]["usage"]["total_tokens"] == 6
