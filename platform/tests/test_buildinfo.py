"""Build stamp behaviour."""

from hypothesis import given
from hypothesis import strategies as st

from common.buildinfo import UNKNOWN, build_info


def test_unstamped_environment_reports_unknown() -> None:
    info = build_info(env={})

    assert info.version == UNKNOWN
    assert info.git_sha == UNKNOWN
    assert info.built_at == UNKNOWN
    assert info.short_sha == UNKNOWN
    assert not info.is_stamped


def test_stamped_environment_is_reported_verbatim() -> None:
    info = build_info(
        env={
            "SENTINEL_VERSION": "0.1.0",
            "SENTINEL_GIT_SHA": "6d438319e8d9ca1abf96ca2ac01f83c4180b5d30",
            "SENTINEL_BUILT_AT": "2026-07-20T09:00:00Z",
        }
    )

    assert info.version == "0.1.0"
    assert info.short_sha == "6d43831"
    assert info.built_at == "2026-07-20T09:00:00Z"
    assert info.is_stamped


@given(st.text(alphabet=" \t\n\r", max_size=8))
def test_blank_values_are_normalized_to_unknown(blank: str) -> None:
    """Whitespace-only stamps are as absent as missing ones — callers see one shape."""
    info = build_info(env={"SENTINEL_GIT_SHA": blank})

    assert info.git_sha == UNKNOWN
    assert not info.is_stamped
