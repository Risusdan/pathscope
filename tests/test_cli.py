import cli


def test_parser_has_both_commands():
    p = cli.build_parser()
    a = p.parse_args(["probe"])
    assert a.cmd == "probe"
    a = p.parse_args(["monitor", "--seconds", "3", "--interval", "10"])
    assert a.cmd == "monitor"
    assert a.seconds == 3.0
