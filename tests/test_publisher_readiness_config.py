"""Production wiring tests for media-specific X readiness configuration."""

from unittest import mock

import main


def _item(kind):
    return {
        "kind": kind,
        "_caption": "caption",
        "_media_path": f"media.{ 'mp4' if kind == 'video' else 'jpg' }",
    }


def test_selected_video_uses_configured_video_readiness_timeout():
    session = mock.Mock()
    session.post.return_value = {"ok": True, "reason": "posted"}
    cfg = {
        "publisher": {
            "image_ready_timeout_seconds": 15,
            "video_ready_timeout_seconds": 240,
        }
    }

    result = main.post_selected_item(session, _item("video"), cfg)

    assert result == {"ok": True, "reason": "posted"}
    session.post.assert_called_once_with(
        "caption",
        ["media.mp4"],
        media_kind="video",
        ready_timeout_s=240,
    )


def test_selected_image_uses_configured_image_readiness_timeout():
    session = mock.Mock()
    cfg = {
        "publisher": {
            "image_ready_timeout_seconds": 15,
            "video_ready_timeout_seconds": 240,
        }
    }

    main.post_selected_item(session, _item("image"), cfg)

    session.post.assert_called_once_with(
        "caption",
        ["media.jpg"],
        media_kind="image",
        ready_timeout_s=15,
    )


def test_old_config_production_wiring_uses_documented_defaults():
    image_session = mock.Mock()
    video_session = mock.Mock()

    main.post_selected_item(image_session, _item("image"), {})
    main.post_selected_item(video_session, _item("video"), {})

    assert image_session.post.call_args.kwargs["ready_timeout_s"] == 60
    assert video_session.post.call_args.kwargs["ready_timeout_s"] == 180
