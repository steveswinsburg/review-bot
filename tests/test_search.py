"""Tests for selecting GitHub repository-owner search qualifiers."""

import sys
from unittest import mock
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import review_bot as rb


@pytest.mark.parametrize(
    ("owner_type", "qualifier"),
    [("Organization", "org"), ("User", "user")],
)
def test_owner_search_qualifier(owner_type, qualifier):
    github = mock.Mock()
    github.get_user.return_value.type = owner_type

    assert rb.owner_search_qualifier(github, "owner-name") == qualifier
    github.get_user.assert_called_once_with("owner-name")


def test_owner_search_qualifier_forced_organization_skips_lookup():
    github = mock.Mock()

    assert rb.owner_search_qualifier(
        github, "org-name", force_organization=True
    ) == "org"
    github.get_user.assert_not_called()


def test_owner_search_qualifier_rejects_unknown_account_type():
    github = mock.Mock()
    github.get_user.return_value.type = "Bot"

    with pytest.raises(ValueError, match="unsupported account type"):
        rb.owner_search_qualifier(github, "owner-name")
