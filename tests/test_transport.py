"""Transports: the simulator, silent modules, ELM327 framing, and J2534."""

from __future__ import annotations

import ctypes

import pytest
from fakes import FakePassThruLibrary, FakeSerialPort

from majster_ai.errors import TransportError, TransportNotOpenError
from majster_ai.mcp_servers.car_interface.elm327 import (
    Elm327Transport,
    parse_elm_response,
)
from majster_ai.mcp_servers.car_interface.j2534 import (
    FLOW_CONTROL_FILTER,
    PassThruMsg,
    RX_STATUS_START_OF_MESSAGE,
    RX_STATUS_TX_MSG_TYPE,
    J2534Transport,
    describe_error,
)
from majster_ai.mcp_servers.car_interface.transport import SilentTransport, SimulatedTransport
from majster_ai.mcp_servers.car_interface.uds_client import UdsSession


class TestSimulatedTransport:
    def test_round_trip(self, ecm) -> None:
        with SimulatedTransport(ecm) as transport:
            transport.send(b"\x3e\x00")
            assert transport.recv(0.1) == b"\x7e\x00"

    def test_recv_returns_none_on_silence(self, ecm) -> None:
        with SimulatedTransport(ecm) as transport:
            assert transport.recv(0.01) is None

    def test_suppressed_response_produces_nothing(self, ecm) -> None:
        with SimulatedTransport(ecm) as transport:
            transport.send(b"\x3e\x80")
            assert transport.recv(0.01) is None

    def test_flush_discards_pending(self, ecm) -> None:
        with SimulatedTransport(ecm) as transport:
            transport.send(b"\x3e\x00")
            transport.flush()
            assert transport.recv(0.01) is None

    def test_closed_transport_refuses_io(self, ecm) -> None:
        transport = SimulatedTransport(ecm)
        with pytest.raises(TransportNotOpenError):
            transport.send(b"\x3e\x00")
        with pytest.raises(TransportNotOpenError):
            transport.recv(0.01)

    def test_describe(self, ecm) -> None:
        described = SimulatedTransport(ecm, request_id=0x7E0, response_id=0x7E8).describe()
        assert described["request_id"] == "0x7E0"
        assert described["open"] is False


class TestSilentTransport:
    """A module that is not fitted: accepts requests, never answers."""

    def test_never_answers(self) -> None:
        with SilentTransport() as transport:
            transport.send(b"\x3e\x00")
            assert transport.recv(0.01) is None

    def test_produces_a_clean_timeout_not_a_crash(self) -> None:
        session = UdsSession(
            SilentTransport(), retries=0, timeout=0.01, sleep=lambda _: None, name="PAM"
        )
        session.open()
        from majster_ai.errors import UdsTimeoutError

        with pytest.raises(UdsTimeoutError):
            session.read_dtc_by_status_mask()

    def test_reason_is_reported(self) -> None:
        transport = SilentTransport(reason="not fitted to this car")
        assert "not fitted" in transport.describe()["reason"]


class TestElmParsing:
    def test_single_frame(self) -> None:
        assert parse_elm_response("62F19053414C\r\r>") == b"\x62\xf1\x90SAL"

    def test_multi_frame_segments_are_ordered(self) -> None:
        raw = "014\r0:62F1905341\r1:4C4641324242\r2:38414831303030\r>"
        assert parse_elm_response(raw).startswith(b"\x62\xf1\x90SAL")

    def test_declared_length_trims_padding(self) -> None:
        raw = "006\r0:62F1905341\r1:4C00000000\r>"
        assert len(parse_elm_response(raw)) == 6

    def test_out_of_order_segments_reassembled(self) -> None:
        raw = "1:4C4641324242\r0:62F1905341\r>"
        assert parse_elm_response(raw).startswith(b"\x62\xf1\x90")

    def test_headers_stripped_when_expected(self) -> None:
        assert (
            parse_elm_response("7E8 62 F1 90 53 41 4C\r>", expect_header=True) == b"\x62\xf1\x90SAL"
        )

    def test_noise_lines_ignored(self) -> None:
        assert parse_elm_response("SEARCHING...\r\r62F190534142\r>") is not None

    @pytest.mark.parametrize(
        ("reply", "fragment"),
        [
            ("NO DATA\r>", "did not answer"),
            ("CAN ERROR\r>", "could not put the frame"),
            ("UNABLE TO CONNECT\r>", "cannot establish"),
            ("BUFFER FULL\r>", "buffer overflowed"),
            ("?\r>", "did not understand"),
        ],
    )
    def test_adapter_errors_explain_the_cause(self, reply: str, fragment: str) -> None:
        with pytest.raises(TransportError, match=fragment):
            parse_elm_response(reply)

    def test_odd_hex_is_rejected_not_truncated(self) -> None:
        # Silently dropping a nibble would corrupt every byte after it.
        with pytest.raises(TransportError, match="odd number"):
            parse_elm_response("62F1905\r>")

    def test_non_hex_rejected(self) -> None:
        with pytest.raises(TransportError):
            parse_elm_response("HELLO WORLD X\r>")

    def test_empty_reply_rejected(self) -> None:
        with pytest.raises(TransportError, match="no usable data"):
            parse_elm_response("\r\r>")


class TestElmTransport:
    @staticmethod
    def _port(extra: dict[str, str] | None = None) -> FakeSerialPort:
        script = {
            "ATZ": "ELM327 v1.5\r",
            "ATE0": "OK\r",
            "ATL0": "OK\r",
            "ATS0": "OK\r",
            "ATH0": "OK\r",
            "ATCAF1": "OK\r",
            "ATSP6": "OK\r",
            "ATSH7E0": "OK\r",
            "ATCRA7E8": "OK\r",
            "ATFCSH7E0": "OK\r",
            "ATFCSD300000": "OK\r",
            "ATFCSM1": "OK\r",
        }
        script.update(extra or {})
        return FakeSerialPort(script)

    def test_initialisation_sequence(self) -> None:
        port = self._port()
        transport = Elm327Transport(lambda: port, sleep=lambda _: None)
        transport.open()
        # Echo off and CAN auto-formatting are the two that break everything
        # else if they are missing.
        assert "ATE0" in port.written
        assert "ATCAF1" in port.written
        assert "ATSH7E0" in port.written
        assert "ATSP6" in port.written
        transport.close()
        assert port.closed

    def test_flow_control_is_configured(self) -> None:
        # Without it, multi-frame responses (i.e. DTC lists) never arrive.
        port = self._port()
        Elm327Transport(lambda: port, sleep=lambda _: None).open()
        assert any(command.startswith("ATFCSH") for command in port.written)
        assert "ATFCSM1" in port.written

    def test_request_response(self) -> None:
        port = self._port({"22F190": "62F19053414C\r"})
        transport = Elm327Transport(lambda: port, sleep=lambda _: None)
        transport.open()
        transport.send(b"\x22\xf1\x90")
        assert transport.recv(1.0) == b"\x62\xf1\x90SAL"

    def test_works_end_to_end_through_a_uds_session(self) -> None:
        port = self._port({"1902FF": "5902FF0299002F\r"})
        transport = Elm327Transport(lambda: port, sleep=lambda _: None)
        session = UdsSession(transport, retries=0, timeout=1.0, sleep=lambda _: None)
        session.open()
        assert [dtc.code for dtc in session.read_dtc_by_status_mask(0xFF)] == ["P0299"]

    def test_clone_rejecting_a_core_command_fails_loudly(self) -> None:
        port = self._port({"ATCAF1": "?\r"})
        transport = Elm327Transport(lambda: port, sleep=lambda _: None)
        with pytest.raises(TransportError, match="rejected 'ATCAF1'"):
            transport.open()

    def test_clone_rejecting_an_optional_command_is_tolerated(self) -> None:
        port = self._port({"ATFCSM1": "?\r", "ATCRA7E8": "?\r"})
        transport = Elm327Transport(lambda: port, sleep=lambda _: None)
        transport.open()  # must not raise
        assert transport.is_open

    def test_port_failure_is_explained(self) -> None:
        def boom() -> FakeSerialPort:
            raise OSError("No such file or directory: /dev/rfcomm0")

        with pytest.raises(TransportError, match="rfcomm bind"):
            Elm327Transport(boom, sleep=lambda _: None).open()

    def test_missing_prompt_times_out_with_advice(self) -> None:
        port = FakeSerialPort({}, default="")
        port.write = lambda data: len(data)  # type: ignore[method-assign]
        transport = Elm327Transport(lambda: port, read_timeout=0.05, sleep=lambda _: None)
        with pytest.raises(TransportError, match="prompt"):
            transport.open()

    def test_io_before_open_rejected(self) -> None:
        transport = Elm327Transport(lambda: FakeSerialPort(), sleep=lambda _: None)
        with pytest.raises(TransportNotOpenError):
            transport.send(b"\x3e\x00")


class TestJ2534:
    def test_struct_round_trip(self) -> None:
        message = PassThruMsg()
        message.set_data(b"\x00\x00\x07\xe0\x22\xf1\x90")
        assert message.get_data() == b"\x00\x00\x07\xe0\x22\xf1\x90"
        assert message.DataSize == 7

    def test_oversized_payload_rejected(self) -> None:
        with pytest.raises(TransportError, match="too long"):
            PassThruMsg().set_data(b"\x00" * 5000)

    def test_open_installs_a_flow_control_filter(self) -> None:
        # Omitting it is the classic reason multi-frame reads silently fail.
        library = FakePassThruLibrary()
        transport = J2534Transport(library)
        transport.open()
        assert library.filters_installed == 1
        assert f"filter:{FLOW_CONTROL_FILTER}" in library.calls
        transport.close()

    def test_send_prefixes_the_can_id(self) -> None:
        library = FakePassThruLibrary()
        transport = J2534Transport(library, request_id=0x7E0)
        transport.open()
        transport.send(b"\x22\xf1\x90")
        assert library.written[0] == b"\x00\x00\x07\xe0\x22\xf1\x90"

    def test_recv_strips_the_can_id(self) -> None:
        library = FakePassThruLibrary(responses=[b"\x00\x00\x07\xe8\x62\xf1\x90ABC"])
        transport = J2534Transport(library, response_id=0x7E8)
        transport.open()
        assert transport.recv(1.0) == b"\x62\xf1\x90ABC"

    def test_loopback_echo_is_discarded(self) -> None:
        """Our own transmission comes back; reading it as the answer would
        make every request appear to succeed with the request as its reply."""
        library = FakePassThruLibrary(responses=[b"\x00\x00\x07\xe0\x22\xf1\x90"])
        original_read = library.PassThruReadMsgs

        def read(channel, message, count, timeout):  # type: ignore[no-untyped-def]
            code = original_read(channel, message, count, timeout)
            if code == 0:
                message._obj.RxStatus = RX_STATUS_TX_MSG_TYPE
            return code

        library.PassThruReadMsgs = read  # type: ignore[method-assign]
        transport = J2534Transport(library)
        transport.open()
        assert transport.recv(0.05) is None

    def test_first_frame_indication_is_discarded(self) -> None:
        library = FakePassThruLibrary(responses=[b"\x00\x00\x07\xe8"])
        original_read = library.PassThruReadMsgs

        def read(channel, message, count, timeout):  # type: ignore[no-untyped-def]
            code = original_read(channel, message, count, timeout)
            if code == 0:
                message._obj.RxStatus = RX_STATUS_START_OF_MESSAGE
            return code

        library.PassThruReadMsgs = read  # type: ignore[method-assign]
        transport = J2534Transport(library)
        transport.open()
        assert transport.recv(0.05) is None

    def test_frame_from_another_module_is_ignored(self) -> None:
        library = FakePassThruLibrary(responses=[b"\x00\x00\x07\xe9\x62\xf1\x90X"])
        transport = J2534Transport(library, response_id=0x7E8)
        transport.open()
        assert transport.recv(0.05) is None

    def test_empty_buffer_times_out(self) -> None:
        transport = J2534Transport(FakePassThruLibrary())
        transport.open()
        assert transport.recv(0.02) is None

    def test_open_failure_is_explained_and_cleans_up(self) -> None:
        library = FakePassThruLibrary(open_error=0x08)
        with pytest.raises(TransportError, match="DEVICE_NOT_CONNECTED"):
            J2534Transport(library).open()

    def test_filter_failure_closes_the_device(self) -> None:
        # Otherwise the next run gets ERR_DEVICE_IN_USE until the cable is pulled.
        library = FakePassThruLibrary()
        library.PassThruStartMsgFilter = lambda *a: 0x17  # type: ignore[method-assign]
        with pytest.raises(TransportError, match="NO_FLOW_CONTROL"):
            J2534Transport(library).open()
        assert "close" in library.calls

    def test_end_to_end_through_a_uds_session(self) -> None:
        library = FakePassThruLibrary(responses=[b"\x00\x00\x07\xe8\x59\x02\xff\x02\x99\x00\x2f"])
        session = UdsSession(J2534Transport(library), retries=0, timeout=0.5, sleep=lambda _: None)
        session.open()
        assert [dtc.code for dtc in session.read_dtc_by_status_mask(0xFF)] == ["P0299"]

    def test_error_descriptions_are_actionable(self) -> None:
        assert "USB cable" in describe_error(0x08)
        assert "another program" in describe_error(0x0E)
        assert "0x99" in describe_error(0x99)

    def test_missing_library_path_is_explained(self) -> None:
        with pytest.raises(TransportError, match="MAJSTER_J2534_LIBRARY"):
            J2534Transport("").open()

    def test_unloadable_library_is_explained(self) -> None:
        with pytest.raises(TransportError, match="word size|Cannot load"):
            J2534Transport("/nonexistent/libop20pt32.so").open()
