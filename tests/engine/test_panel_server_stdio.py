import json

from tools.panel_server.panel_server_stdio import handle_request


def test_handle_request_dispatches_to_the_matching_method_and_returns_result():
    methods = {"double": lambda args: args[0] * 2}
    line = json.dumps({"id": 1, "method": "double", "args": [21]})

    response = handle_request(line, methods=methods)

    assert response == {"id": 1, "result": 42}


def test_handle_request_returns_an_error_for_an_unknown_method():
    response = handle_request(
        json.dumps({"id": 2, "method": "nope", "args": []}), methods={}
    )
    assert response == {"id": 2, "error": "Unknown method: nope"}


def test_handle_request_catches_an_exception_from_the_method_and_returns_an_error():
    def boom(args):
        raise RuntimeError("panel exploded")

    response = handle_request(
        json.dumps({"id": 3, "method": "boom", "args": []}),
        methods={"boom": boom},
    )
    assert response == {"id": 3, "error": "panel exploded"}


def test_handle_request_defaults_args_to_an_empty_list_when_missing():
    methods = {"noop": lambda args: args}
    response = handle_request(
        json.dumps({"id": 4, "method": "noop"}), methods=methods
    )
    assert response == {"id": 4, "result": []}


def test_methods_table_has_exactly_the_expected_entries():
    from tools.panel_server.panel_server_stdio import METHODS
    assert set(METHODS.keys()) == {
        "led_panel_run", "led_panel_query",
        "dual_panel_turn_all_leds_on", "dual_panel_turn_all_leds_off",
        "dual_panel_start_scanning", "dual_panel_stop_scanning",
        "dual_panel_enter_stream_panel", "dual_panel_exit_stream_panel",
    }
