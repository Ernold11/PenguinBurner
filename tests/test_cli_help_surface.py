import pytest

from cli.arguments import parse_arguments
from stability.q2rtx.cli import parse_q2rtx_stability_args


def _help_output(capsys, parser, argv=None):
    with pytest.raises(SystemExit) as exc:
        parser(["--help"] if argv is None else argv)
    assert exc.value.code == 0
    return capsys.readouterr().out


def test_main_cli_help_hides_default_and_unclear_compat_flags(capsys):
    help_text = _help_output(capsys, parse_arguments)

    assert "--foreground" not in help_text
    assert "--power-limit-override-w" not in help_text
    assert "--auto-uv-power-limit-w" in help_text


def test_main_cli_help_removes_old_compat_flags(capsys):
    help_text = _help_output(capsys, parse_arguments)

    removed_flags = [
        "--foreground",
        "--power-limit-override-w",
        "--show-q2rtx-window",
        "--stability-q2rtx-dir",
        "--stability-q2rtx-binary",
        "--prefer-afterburner-curve",
        "--restore-defaults-from-config",
        "--preserve-vf-below-mv",
        "--dangerously-skip-validation",
    ]
    for flag in removed_flags:
        assert flag not in help_text


def test_standalone_q2rtx_help_hides_custom_binary_debug_flags(capsys):
    help_text = _help_output(capsys, parse_q2rtx_stability_args)

    assert "--show-q2rtx-window" not in help_text
    assert "--stability-q2rtx-dir" not in help_text
    assert "--stability-q2rtx-binary" not in help_text


@pytest.mark.parametrize(
    "argv",
    [
        ["--foreground"],
        ["--power-limit-override-w", "320"],
        ["--show-q2rtx-window"],
        ["--stability-q2rtx-dir", "/tmp/q2rtx"],
        ["--stability-q2rtx-binary", "/tmp/q2rtx/q2rtx"],
        ["--prefer-afterburner-curve"],
        ["--restore-defaults-from-config"],
        ["--preserve-vf-below-mv", "800"],
        ["--dangerously-skip-validation"],
    ],
)
def test_removed_main_cli_flags_are_rejected(argv):
    with pytest.raises(SystemExit) as exc:
        parse_arguments(argv)
    assert exc.value.code == 2


def test_auto_uv_power_limit_flag_is_accepted():
    args = parse_arguments(["--auto-uv-power-limit-w", "390"])

    assert args.auto_uv_power_limit_w == 390


@pytest.mark.parametrize(
    "argv",
    [
        ["--show-q2rtx-window"],
        ["--stability-q2rtx-dir", "/tmp/q2rtx"],
        ["--stability-q2rtx-binary", "/tmp/q2rtx/q2rtx"],
    ],
)
def test_removed_standalone_q2rtx_flags_are_rejected(argv):
    with pytest.raises(SystemExit) as exc:
        parse_q2rtx_stability_args(argv)
    assert exc.value.code == 2
