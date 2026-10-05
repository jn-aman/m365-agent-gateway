from urllib.parse import parse_qs, urlsplit

import pytest
from test_sessions import socket_url

from m365_agent_gateway.errors import GatewayError
from m365_agent_gateway.normalize import ImageData
from m365_agent_gateway.sessions import Session
from m365_agent_gateway.upstream import build_request, decode_frames, response_update, upload_fields


def test_fresh_request_isolates_conversations():
    session = Session.from_capture(socket_url(), {"source": "officeweb", "message": {}})
    first_url, first_frame = build_request(session, "hello", "Claude_Sonnet")
    second_url, second_frame = build_request(session, "hello", "Magic")
    assert first_url != second_url
    assert first_frame["arguments"][0]["message"]["text"] == "hello"
    assert first_frame["arguments"][0]["tone"] == "Claude_Sonnet"
    first_conversation = parse_qs(urlsplit(first_url).query)["ConversationId"][0]
    second_conversation = parse_qs(urlsplit(second_url).query)["ConversationId"][0]
    assert first_conversation != second_conversation
    assert first_frame["arguments"][0]["sessionId"] != second_frame["arguments"][0]["sessionId"]


def test_image_chat_binds_uploads_via_message_annotations():
    session = Session.from_capture(socket_url(), {"source": "officeweb", "optionsSets": []})
    annotation = {"id": "doc-1", "messageAnnotationType": "ImageFile"}
    _, frame = build_request(session, "describe", "Claude_Sonnet", annotations=[annotation])
    argument = frame["arguments"][0]
    assert "gptvnorm2048" in argument["optionsSets"]
    assert "flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch" in argument["optionsSets"]
    assert argument["message"]["messageAnnotations"] == [annotation]
    _, plain = build_request(session, "describe", "Claude_Sonnet")
    assert "messageAnnotations" not in plain["arguments"][0]["message"]


def test_image_upload_fields_match_har_contract():
    image = ImageData("image/png", "aW1hZ2U=")
    fields = upload_fields("conversation-id", image)
    assert fields == [
        ("scenario", "UploadImage"),
        ("conversationId", "conversation-id"),
        ("FileBase64", "data:image/png;base64,aW1hZ2U="),
        ("optionsSets", "cwcgptvsan"),
        ("optionsSets", "flux_v3_gptv_enable_upload_multi_image_in_turn_wo_ch"),
        ("optionsSets", "gptvnorm2048"),
    ]


def test_signalr_multiple_frames():
    assert decode_frames('{}\x1e{"type":6}\x1e') == [{}, {"type": 6}]
    with pytest.raises(GatewayError):
        decode_frames("not-json\x1e")


def test_bot_updates_exclude_user_echo():
    frame = {
        "type": 1,
        "arguments": [
            {
                "messages": [
                    {"author": "user", "text": "private"},
                    {"author": "bot", "text": "answer"},
                ]
            }
        ],
    }
    assert response_update(frame) == ("answer", False)


def test_progress_and_chain_of_thought_updates_are_not_user_output():
    progress = {
        "type": 1,
        "arguments": [
            {"messages": [{"author": "bot", "messageType": "Progress", "text": "Working"}]}
        ],
    }
    thought = {
        "type": 1,
        "arguments": [
            {
                "messages": [
                    {
                        "author": "bot",
                        "contentOrigin": "ChainOfThoughtSummary",
                        "addToChainOfThought": True,
                        "text": "Private reasoning",
                    }
                ]
            }
        ],
    }
    answer = {
        "type": 1,
        "arguments": [
            {"messages": [{"author": "bot", "contentOrigin": "DeepLeo", "text": "Answer"}]}
        ],
    }
    assert response_update(progress) == (None, False)
    assert response_update(thought) == (None, False)
    assert response_update(answer) == ("Answer", False)


def test_completion_and_upstream_failure():
    assert response_update(
        {
            "type": 2,
            "item": {
                "messages": [
                    {"author": "bot", "text": "done"},
                ]
            },
        }
    ) == ("done", True)
    with pytest.raises(GatewayError):
        response_update(
            {"type": 2, "item": {"result": {"value": "Unauthorized", "message": "secret"}}}
        )


def test_final_streaming_flag():
    assert response_update(
        {
            "type": 1,
            "arguments": [{"isFinal": True, "messages": [{"author": "bot", "text": "done"}]}],
        }
    ) == ("done", True)
