# scripts/tests/test_publisher.py
import pytest
from unittest.mock import patch, MagicMock, call
import logging
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import publisher

logger = logging.getLogger("test")
REPO = Path("/home/muklis/Documents/exploring/blog")


def make_pub():
    return publisher.GitPublisher(REPO, logger)


def test_publish_nothing_when_no_files():
    pub = make_pub()
    with patch("subprocess.run") as mock_run:
        pub.publish([], [], "2026-03-23")
    mock_run.assert_not_called()


def test_publish_calls_git_add_commit_push():
    pub = make_pub()
    post = REPO / "_posts/2026-03-23-test.md"
    mock_ok = MagicMock(returncode=0, stdout="file.md\n", stderr="")
    with patch("subprocess.run", return_value=mock_ok) as mock_run, \
         patch("pathlib.Path.exists", return_value=True):
        pub.publish([post], [], "2026-03-23")
    commands = [c.args[0] for c in mock_run.call_args_list]
    assert any("add" in cmd for cmd in commands)
    assert any("commit" in cmd for cmd in commands)
    assert any("push" in cmd for cmd in commands)


def test_publish_raises_on_git_failure():
    pub = make_pub()
    post = REPO / "_posts/2026-03-23-test.md"
    mock_fail = MagicMock(returncode=1, stdout="", stderr="push failed")
    mock_ok = MagicMock(returncode=0, stdout="file.md\n", stderr="")

    def side_effect(cmd, **kwargs):
        if "push" in cmd:
            return mock_fail
        return mock_ok

    with patch("subprocess.run", side_effect=side_effect), \
         patch("pathlib.Path.exists", return_value=True):
        with pytest.raises(publisher.GitPublishError):
            pub.publish([post], [], "2026-03-23")


def test_publish_skips_commit_when_nothing_staged():
    pub = make_pub()
    post = REPO / "_posts/2026-03-23-test.md"
    empty_staged = MagicMock(returncode=0, stdout="", stderr="")
    mock_ok = MagicMock(returncode=0, stdout="", stderr="")

    def side_effect(cmd, **kwargs):
        if "diff" in cmd:
            return empty_staged
        return mock_ok

    with patch("subprocess.run", side_effect=side_effect) as mock_run, \
         patch("pathlib.Path.exists", return_value=True):
        pub.publish([post], [], "2026-03-23")

    commands = [c.args[0] for c in mock_run.call_args_list]
    assert not any("push" in cmd for cmd in commands)
