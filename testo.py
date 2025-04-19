#!/usr/bin/env python3

import subprocess
import sys
import threading
import queue
import math
from os import get_terminal_size
from termcolor import colored, cprint
from time import sleep
from sys import platform
import signal
import socket
import select
import re
import pty
import os

from UDPTranslator import translateMessage, getMessageId
from UDPMessages import confirm, reply, auth, join, msg, err, bye, ping

global debug
global run_tcp
global run_udp
global test

# Define a global list to hold all test case functions
udp_test_cases = []
tcp_test_cases = []


class TimeoutError(Exception):
    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


def testcase(func):
    def wrapper(tester, *args, **kwargs):
        passed = False

        title = f" ⏳ Starting test '{func.__name__}' "
        start_sep = "=" * math.floor((get_terminal_size().columns - len(title) - 1) / 2)
        end_sep = "=" * (
            get_terminal_size().columns - (len(start_sep) + len(title)) - 1
        )
        print(colored("\n" + start_sep + title + end_sep, "yellow"))
        try:
            func(tester, *args, **kwargs)
            print(colored(f"✅ Test '{func.__name__}': PASSED", "green"))
            passed = True
        except AssertionError as e:
            print(colored(f"❌ Test '{func.__name__}': FAILED - {e}", "red"))
            tester.dump()
        except TimeoutError as e:
            print(colored(f"❌ Test '{func.__name__}': FAILED - Timeout - {e}", "red"))
            tester.dump()
        except Exception as e:
            print(colored(f"❌ Test '{func.__name__}': ERROR - {e}", "red"))
        print(colored(f"Test '{func.__name__}' finished", "yellow"))
        tester.teardown()  # Clean up after test

        return passed

    # pass in the testcase name
    wrapper_func = wrapper
    wrapper_func.__name__ = func.__name__

    # Register the test case
    if "udp" in func.__name__:
        udp_test_cases.append(wrapper_func)
    elif "tcp" in func.__name__:
        tcp_test_cases.append(wrapper_func)
    return wrapper


class ExecutableTester:
    def __init__(self, executable_path):
        self.executable_path = executable_path
        self.process = None
        self.stdout_queue = queue.Queue()
        self.stderr_queue = queue.Queue()
        self.return_code = None
        self.server_socket = None
        self.connection_socket = None  # For TCP connections
        self.client_address = None  # For UDP responses
        self.send_confirm = True
        self.history = ""
        self.accept_thread = None  # Keep track of the thread accepting connections

    def start_server(self, protocol, port):
        if protocol.lower() == "tcp":
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_REUSEADDR, 1
            )  # Set SO_REUSEADDR
            self.server_socket.bind(("localhost", port))
            self.server_socket.listen(1)
            # Start a new thread to run the blocking accept call
            thread = threading.Thread(target=self.accept_connection)
            thread.daemon = True  # Daemon threads exit when the main program does
            thread.start()

        elif protocol.lower() == "udp":
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.server_socket.bind(("localhost", port))

    def accept_connection(self):
        self.connection_socket, _ = self.server_socket.accept()

    def stop_server(self):
        if self.connection_socket:
            self.connection_socket.close()
        if self.server_socket:
            self.server_socket.close()
        if self.accept_thread:  # Ensure the accept thread has finished
            self.accept_thread.join()

    def send_message(self, message):
        if self.connection_socket:  # TCP
            self.connection_socket.sendall(message.encode())
        elif self.server_socket and self.client_address:  # UDP
            self.server_socket.sendto(message, self.client_address)

    def receive_message(self, timeout=5):
        if self.server_socket:
            self.server_socket.settimeout(timeout)
            try:
                if self.connection_socket:  # TCP
                    return self.connection_socket.recv(1024).decode()
                else:  # UDP
                    message, self.client_address = self.server_socket.recvfrom(1024)
                    if message[0] != 0:
                        # we should confirm it directly
                        if self.send_confirm:
                            self.confirm(message)
                    return message
            except socket.timeout:
                raise TimeoutError("Socket timed out.")

    def setup(self, args=["-t", "udp", "-s", "localhost", "-p", "4567"]):
        if self.process:
            self.teardown()
        self.stdout_queue = queue.Queue()
        self.stderr_queue = queue.Queue()

        if platform == "linux" or platform == "linux2":

            self.process = subprocess.Popen(
                ["stdbuf", "-o0", self.executable_path] + args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self._start_thread(self.read_stdout, self.stdout_queue)
            self._start_thread(self.read_stderr, self.stderr_queue)
        elif platform == "darwin":
            master, slave = pty.openpty()  # Open a pseudo-terminal pair

            self.process = subprocess.Popen(
                [self.executable_path] + args,
                stdin=subprocess.PIPE,
                stdout=slave,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=0,  # Set small buffer size
            )

            os.close(slave)  # Close the slave fd, the subprocess will write to it
            self.stdout_fd = master  # Use the master for reading output

            self._start_thread(self.buffer_stdout_fd, self.stdout_queue)
            self._start_thread(self.read_stdout, self.stdout_queue)

        self.return_code = None

        sleep(0.2)  # Give some time for the process to start

    def _start_thread(self, target, queue):
        thread = threading.Thread(target=target, args=(queue,))
        thread.daemon = True  # Thread dies with the program
        thread.start()

    def buffer_stdout_fd(self, queue):
        while True:
            if self.stdout_fd and self.process and self.process.poll() is not None:
                break  # Subprocess has terminated, exit the loop
            try:
                line = os.read(self.stdout_fd, 1024).decode("utf-8")
                if line:
                    if debug:
                        print(
                            colored("STDOUT:", "blue") + colored(line, "blue"), end=""
                        )
                    queue.put(line)
            except Exception as e:
                print(f"Error reading from pty: {e}")
                break

    def read_stdout(self, queue):
        for line in iter(self.process.stdout.readline, ""):
            if debug:
                print(colored("STDOUT:", "blue"), colored(line, "blue"), end="")
            queue.put(line)

    def read_stderr(self, queue):
        for line in iter(self.process.stderr.readline, ""):
            if debug:
                print(colored("stderr:", "magenta"), colored(line, "magenta"), end="")
            queue.put(line)

    def execute(self, input_data):
        self.history += "stdin:" + input_data + "\n"
        self.process.stdin.write(input_data + "\n")
        self.process.stdin.flush()

        sleep(0.2)

    def get_stdout(self):
        output = []
        while not self.stdout_queue.empty():
            output.append(self.stdout_queue.get())
        self.history += "stdout:".join(output)
        return "".join(output)

    def get_stderr(self):
        output = []
        while not self.stderr_queue.empty():
            output.append(self.stderr_queue.get())
        return "".join(output)

    def teardown(self):
        if self.process:
            self.process.terminate()
            self.process.wait()
            self.return_code = self.process.returncode
            self.process = None

        self.stop_server()
        self.server_socket = None
        self.connection_socket = None

    def confirm(self, message):
        self.send_message(b"\x00" + getMessageId(message))

    def get_return_code(self):
        return self.return_code

    def send_signal(self, signal):
        self.process.send_signal(signal)

    def send_eof(self):
        self.process.stdin.close()

    def dump(self):

        print(colored("stdout now", "magenta"))
        print(self.get_stdout())

        print(colored("stdout", "magenta"))
        print(self.get_stdout())

        print(colored("stderr", "magenta"))
        print(self.get_stderr())

        print(colored("History", "magenta"))
        print(self.history)

    def setClientAddress(self, client_address):
        self.client_address = client_address


### TEST CASES ###


# PART 1 - Testing command-line aguments


@testcase
def no_args(tester):
    """Test that the program exits with a non-zero exit code when no arguments are provided"""
    tester.setup(args=[])
    assert tester.get_return_code() != 0, "Expected non-zero exit code."


@testcase
def no_type_arg(tester):
    """Test that the program exits with a non-zero exit code when the -t argument is not provided."""
    tester.setup(args=["-s", "localhost"])
    assert tester.get_return_code() != 0, "Expected non-zero exit code."


@testcase
def no_hostname(tester):
    """Test that the program exits with a non-zero exit code when the -s argument is not provided."""
    tester.setup(args=["-t", "udp"])
    assert tester.get_return_code() != 0, "Expected non-zero exit code."


@testcase
def all_args(tester):
    """Test that the program exits with a non-zero exit code when the -s argument is not provided."""
    tester.setup(args=["-t", "udp", "-s", "localhost", "-p", "4567"])
    tester.send_eof()
    assert tester.get_return_code() == None, "Expected zero exit code."


# PART 2: UDP

@testcase
def udp_help_command(tester):
    """Test that the /help command shows some output."""
    auth_and_reply(tester)
    tester.execute("/help")
    sleep(0.2)
    stdout = tester.get_stdout()
    # Check for any output after the command
    assert len(stdout.strip()) > 0, "Expected some output for /help command."

@testcase
def udp_hello(tester):
    """Test that the program does not accept any message commands before the user is authenticated."""
    tester.setup(args=["-t", "udp", "-s", "localhost", "-p", "4567"])

    # Invalid command for the START state
    tester.execute("Hello")

    stdout = tester.get_stdout()
    
    assert "ERROR: " in stdout, "Output does not match expected output."


@testcase
def udp_not_auth(tester):
    """Test that the program does not accept any join commands before the user is authenticated."""
    tester.start_server("udp", 4567)
    tester.setup(args=["-t", "udp", "-s", "localhost", "-p", "4567"])
    tester.execute("/join")

    # The ERR message should be printed out exactly like this
    stdout = tester.get_stdout()
    assert any(
        ["ERROR:" in line for line in stdout.split("\n")]
    ), "Output does not match expected error message."


@testcase
def udp_invalid_command(tester):
    """Test that the program does not accept invalid commands."""
    tester.setup(args=["-t", "udp", "-s", "localhost", "-p", "4567"])

    # Invalid command in general
    tester.execute("/pepe")

    stdout = tester.get_stdout()
    
    assert "ERROR:" in stdout, "Output does not match expected output."


@testcase
def udp_auth(tester):
    """Test that the program sends the correct AUTH message."""
    tester.start_server("udp", 4567)
    tester.setup(args=["-t", "udp", "-s", "localhost", "-p", "4567"])

    # Send AUTH command
    tester.execute("/auth a b c")

    # Check AUTH message received
    message = tester.receive_message()
    tMessage = translateMessage(message)
    assert (
        tMessage == "AUTH IS a AS c USING b\r\n"
    ), "Incoming message does not match expected message."


@testcase
def udp_auth_port(tester):
    """Test that the program sends the correct AUTH message when the port is non-default."""
    tester.start_server("udp", 1234)
    tester.setup(args=["-t", "udp", "-s", "localhost", "-p", "1234"])

    # Send AUTH command
    tester.execute("/auth a b c")

    # Check AUTH message received
    message = tester.receive_message()
    tMessage = translateMessage(message)
    assert (
        tMessage == "AUTH IS a AS c USING b\r\n"
    ), "Incoming message does not match expected message."


@testcase
def udp_auth_nok(tester):
    """Test that the program handles a NOK reply to AUTH correctly."""
    tester.start_server("udp", 4567)
    tester.setup(args=["-t", "udp", "-s", "localhost", "-p", "4567"])

    # Send AUTH command
    tester.execute("/auth a b c")

    # Expect the auth message to be received by the server
    message = tester.receive_message()
    tMessage = translateMessage(message)
    assert (
        tMessage == "AUTH IS a AS c USING b\r\n"
    ), "Incoming message does not match expected AUTH message."

    # Reply with NOK
    tester.send_message(reply(0, False, getMessageId(message), "nene"))

    sleep(0.2)

    # Check the output, should contain "Action Failure: nene"
    stdout = tester.get_stdout()
    assert any(
        ["Action Failure: nene" in line for line in stdout.split("\n")]
    ), "Output does not match expected 'Action Failure: nene' output."

    # Should receive CONFIRM for the REPLY message
    message = tester.receive_message()
    assert (
        message == b"\x00\x00\x00"
    ), "Incoming message does not match expected CONFIRM message."

@testcase
def udp_rename_multiple(tester):
    """Test renaming multiple times and sending a message."""
    auth_and_reply(tester) # Initial auth as 'c'
    tester.execute("/rename user1")
    sleep(0.2)
    tester.execute("/rename user2")
    sleep(0.2)
    tester.execute("message after rename")

    # Expect MSG with the latest display name 'user2'
    message = tester.receive_message()
    tMessage = translateMessage(message)
    assert (
        tMessage == "MSG FROM user2 IS message after rename\r\n"
    ), "Message should be sent with the latest display name 'user2'."


@testcase
def udp_auth_nok_ok(tester):
    """Test that the program handles a NOK reply to AUTH followed by a Action successful AUTH correctly."""
    tester.start_server("udp", 4567)
    tester.setup(args=["-t", "udp", "-s", "localhost", "-p", "4567"])

    # Send AUTH command
    tester.execute("/auth a b c")

    # Expect the auth message to be received by the server
    message = tester.receive_message()
    tMessage = translateMessage(message)
    assert (
        tMessage == "AUTH IS a AS c USING b\r\n"
    ), "Incoming message does not match expected AUTH message."

    # Reply with NOK
    tester.send_message(reply(0, False, getMessageId(message), "nene"))

    sleep(0.2)

    # Check the output, should contain "Action Failure: nene"
    stdout = tester.get_stdout()
    assert any(
        ["Action Failure: nene" in line for line in stdout.split("\n")]
    ), "Output does not match expected 'Action Failure: nene' output."

    # Should receive CONFIRM for the REPLY message
    message = tester.receive_message()
    assert (
        message == b"\x00\x00\x00"
    ), "Incoming message does not match expected CONFIRM message."

    # Send a second AUTH message
    tester.execute("/auth a b c")

    # Expect the auth message to be received by the server
    message = tester.receive_message()
    tMessage = translateMessage(message)
    assert (
        tMessage == "AUTH IS a AS c USING b\r\n"
    ), "Incoming message does not match expected AUTH message."

    # Reply with OK
    tester.send_message(reply(1, True, getMessageId(message), "yes"))

    sleep(0.2)

    # Check the output, should contain "Action Success: yes"
    stdout = tester.get_stdout()
    assert any(
        ["Action Success: yes" in line for line in stdout.split("\n")]
    ), "Output does not match expected 'Action Success: yes' output."

    # Should receive CONFIRM for the REPLY message
    message = tester.receive_message()
    assert (
        message == b"\x00\x00\x01"
    ), "Incoming message does not match expected CONFIRM message."


@testcase
def udp_auth_port_change(tester):
    """Test that the program switches to the new port after receiving an OK reply to AUTH."""
    # Start initial port listener
    tmp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tmp_socket.bind(("localhost", 1234))

    # Start normal port listener
    tester.start_server("udp", 4567)

    # Start client on temp port
    tester.setup(args=["-t", "udp", "-s", "localhost", "-p", "1234"])

    # Send AUTH command
    tester.execute("/auth a b c")

    # Expect the auth message to be received by the server on tmp socket
    tmp_socket.settimeout(10)
    message, client_address = tmp_socket.recvfrom(1024)
    # Confirm the AUTH message on tmp socket
    tmp_socket.sendto(b"\x00" + getMessageId(message), client_address)
    tMessage = translateMessage(message)
    assert (
        tMessage == "AUTH IS a AS c USING b\r\n"
    ), "Incoming message does not match expected AUTH message."

    # Reply with OK from different port (from now on the client should switch to it)
    tester.setClientAddress(client_address)
    tester.send_message(reply(0, True, getMessageId(message), "jojo"))

    sleep(0.2)

    # Check the output, should contain "Action Success: jojo"
    stdout = tester.get_stdout()
    assert any(
        ["Action Success: jojo" in line for line in stdout.split("\n")]
    ), "Output does not match expected 'Action Success: jojo' output."

    # Should receive CONFIRM for the REPLY message
    message = tester.receive_message()
    assert (
        message == b"\x00\x00\x00"
    ), "Incoming message does not match expected CONFIRM message."


# Helper function
def auth_and_reply(tester):
    """Helper function to test the AUTH command followed by a Action successful reply."""
    tester.start_server("udp", 4567)
    tester.setup(args=["-t", "udp", "-s", "localhost", "-p", "4567"])

    # Send AUTH command
    tester.execute("/auth a b c")

    # Expect the auth message to be received by the server
    message = tester.receive_message()
    tMessage = translateMessage(message)
    assert (
        tMessage == "AUTH IS a AS c USING b\r\n"
    ), "Incoming message does not match expected AUTH message."

    # Reply with OK
    tester.send_message(reply(0, True, getMessageId(message), "jojo"))

    sleep(0.2)

    # Check the output, should contain "Action Success: jojo"
    stdout = tester.get_stdout()
    assert any(
        ["Action Success: jojo" in line for line in stdout.split("\n")]
    ), "Output does not match expected 'Action Success: jojo' output."

    # Should receive CONFIRM for the REPLY message
    message = tester.receive_message()
    assert (
        message == b"\x00\x00\x00"
    ), "Incoming message does not match expected CONFIRM message."


@testcase
def udp_auth_ok(tester):
    """Test that the program handles a Action successful reply to AUTH correctly."""
    auth_and_reply(tester)


@testcase
def udp_msg(tester):
    """Test that the program sends the correct MSG message."""
    auth_and_reply(tester)

    tester.execute("ahojky")

    # Expect the message to be received by the server
    message = tester.receive_message()
    tMessage = translateMessage(message)
    assert (
        tMessage == "MSG FROM c IS ahojky\r\n"
    ), "Incoming message does not match expected MSG message."


@testcase
def udp_svr_msg(tester):
    """Test that the program handles a MSG message from the server correctly."""
    auth_and_reply(tester)

    # Send a message from the server
    tester.send_message(msg(1, "smrt", "ahojky"))

    sleep(0.2)

    # Check the output, should contain "ahojky"
    stdout = tester.get_stdout()
    assert any(
        ["smrt: ahojky" in line for line in stdout.split("\n")]
    ), "Output does not match expected output."

    # Should receive CONFIRM for the MSG message
    message = tester.receive_message()
    assert (
        message == b"\x00\x00\x01"
    ), "Incoming message does not match expected CONFIRM message."

# New test for IPK25
@testcase
def udp_ping(tester):
    """Test that the program handles PING correctly."""
    auth_and_reply(tester)
    
    tester.send_message(ping(1))


    message = tester.receive_message()
    assert (
        message == b"\x00\x00\x01"
    ), "Incoming message does not match expected CONFIRM message."


@testcase
def udp_bye1(tester):
    """Test that the program handles SIGINT correctly."""
    auth_and_reply(tester)

    # Send a message from the server
    tester.send_signal(signal.SIGINT)

    message = tester.receive_message()
    tMessage = translateMessage(message)

    assert (
        tMessage == "BYE FROM c\r\n"
    ), "Incoming message does not match expected BYE message."


@testcase
def udp_bye2(tester):
    """Test that the program handles a C-d (stdin closed) correctly."""
    auth_and_reply(tester)

    # Send a message from the server
    tester.process.stdin.close()

    message = tester.receive_message()
    tMessage = translateMessage(message)
    assert (
        tMessage == "BYE FROM c\r\n"
    ), "Incoming message does not match expected BYE message."

@testcase
def udp_server_bye(tester):
    """Test that the client terminates correctly upon receiving BYE from the server."""
    auth_and_reply(tester)
    tester.send_message(bye(1, "c"))
    sleep(0.2)
    # check if the process is no longer running
    assert tester.process.poll() is not None, "Client process should terminate after receiving BYE."
    # We should also receive a CONFIRM for the BYE message
    message = tester.receive_message()
    assert (
        message == b"\x00\x00\x01"
    ), "Incoming message does not match expected CONFIRM message for BYE."

@testcase
def udp_send_receive_multiple(tester):
    """Test sending and receiving multiple messages."""
    auth_and_reply(tester)
    tester.execute("client msg 1")
    message = tester.receive_message()
    assert "MSG FROM c IS client msg 1\r\n" in translateMessage(message), "Expected client msg 1"

    tester.send_message(msg(1, "server", "server msg 1"))
    sleep(0.2)
    stdout = tester.get_stdout()
    assert "server: server msg 1" in stdout, "Expected server msg 1 in output"
    message = tester.receive_message() # Confirm for server msg 1
    assert message == b"\x00\x00\x01", "Expected confirm for server msg 1"

    tester.execute("client msg 2")
    message = tester.receive_message()
    assert "MSG FROM c IS client msg 2\r\n" in translateMessage(message), "Expected client msg 2"

    tester.send_message(msg(2, "server", "server msg 2"))
    sleep(0.2)
    stdout = tester.get_stdout()
    assert "server: server msg 2" in stdout, "Expected server msg 2 in output"
    message = tester.receive_message() # Confirm for server msg 2
    assert message == b"\x00\x00\x02", "Expected confirm for server msg 2"

@testcase
def udp_retransmit_missing_confirm1(tester):
    """Test that the client retransmits a message if CONFIRM is not received."""
    # Authenticate first
    auth_and_reply(tester)

    tester.send_confirm = False
    # Client sends a message
    tester.execute("test retransmit message")

    # Receive the message, but DO NOT confirm it
    message1 = tester.receive_message(timeout=1) 
    msg_id1 = getMessageId(message1)
    assert "MSG FROM c IS test retransmit message\r\n" in translateMessage(message1), "Expected first message"

    # Wait for potential retransmit
    try:
        message2 = tester.receive_message(timeout=3) 
        msg_id2 = getMessageId(message2)
        assert "MSG FROM c IS test retransmit message\r\n" in translateMessage(message2), "Expected retransmitted message"
        assert msg_id1 == msg_id2, "Retransmitted message ID should be the same"
        # Now confirm the retransmitted message
        tester.confirm(message2)
    except TimeoutError:
        assert False, "Client did not retransmit the message within the timeout period."


@testcase
def udp_retransmit_missing_confirm2(tester):
    """Test that the client retransmits a message if CONFIRM is not received."""
    tester.start_server("udp", 4567)
    tester.setup(args=["-t", "udp", "-s", "localhost", "-p", "4567"])
    tester.execute("/auth a b c")

    # Receive AUTH, but DO NOT confirm it
    tester.send_confirm = False
    message1 = tester.receive_message(timeout=1) 
    msg_id1 = getMessageId(message1)
    assert "AUTH IS a AS c USING b\r\n" in translateMessage(message1), "Expected AUTH message"

    # Wait for potential retransmit
    try:
        message2 = tester.receive_message(timeout=3) 
        msg_id2 = getMessageId(message2)
        assert "AUTH IS a AS c USING b\r\n" in translateMessage(message2), "Expected retransmitted AUTH message"
        assert msg_id1 == msg_id2, "Retransmitted message ID should be the same"
        # Now confirm the retransmitted message
        tester.confirm(message2)
    except TimeoutError:
        assert False, "Client did not retransmit the message within the timeout period."

@testcase
def udp_ignore_duplicate_server_msg(tester):
    """Test that the client ignores duplicated messages from the server but confirms them."""
    auth_and_reply(tester)

    # Send the same message twice
    server_message = msg(1, "server", "duplicate test")
    tester.send_message(server_message)
    sleep(0.2)
    tester.send_message(server_message) # Send duplicate
    sleep(0.2)

    # Check output - should only contain the message once
    stdout = tester.get_stdout()
    occurrences = stdout.count("server: duplicate test")
    assert occurrences == 1, f"Expected message to appear once in output, found {occurrences} times."

    # Check confirmations - should receive two confirms for the same message ID
    confirm1 = tester.receive_message()
    assert confirm1 == b"\x00\x00\x01", "Expected confirm for first message"
    confirm2 = tester.receive_message()
    assert confirm2 == b"\x00\x00\x01", "Expected confirm for duplicate message"

@testcase
def udp_reports_error_when_missing_confirmation(tester):
    """Test that the client retransmits a message if CONFIRM is not received and also Error when client dont recieve CONFIRM."""
    auth_and_reply(tester)

    # Receive AUTH, but DO NOT confirm it
    tester.send_confirm = False
    tester.execute("Hello!")
    sleep(2)
    stdout = tester.get_stdout()
    print(stdout)
    assert any(
        ["ERROR: " in line for line in stdout.split("\n")]
    ), "Output does not match expected 'ERROR: ' output."
    
    
    

@testcase
def udp_server_err1(tester):
    """Test that the program handles an ERR message from the server correctly while waiting for REPLY."""
    tester.start_server("udp", 4567)
    tester.setup(args=["-t", "udp", "-s", "localhost", "-p", "4567"])

    tester.execute("/auth a b c")

    # Should have received the AUTH message
    message = tester.receive_message()
    tMessage = translateMessage(message)
    assert (
        tMessage == "AUTH IS a AS c USING b\r\n"
    ), "Incoming message does not match expected AUTH message."

    sleep(0.2)

    # Send an ERR message from the server
    tester.send_message(err(0, "server", "chyba"))

    sleep(0.2)

    stdout = tester.get_stdout()
    assert any(
        ["ERROR FROM server: chyba" in line for line in stdout.split("\n")]
    ), "Output does not match expected error message."

    # Should receive CONFIRM for the ERR message
    message = tester.receive_message()
    assert (
        message == b"\x00\x00\x00"
    ), "Incoming message does not match expected CONFIRM message."

    # Should receive BYE for the ERR message
    # !!!Based on new FSM BYE is not mandatory anymore!!!
    # message = tester.receive_message()
    # tMessage = translateMessage(message)
    # assert (
    #     tMessage == "BYE FROM c\r\n"
    # ), "Incoming message does not match expected BYE message."


@testcase
def udp_server_err2(tester):
    """Test that the program handles an ERR message from the server correctly."""
    auth_and_reply(tester)

    # Send a message from the server
    tester.send_message(err(1, "server", "chyba"))

    sleep(0.2)

    # The ERR message should be printed out exactly like this
    stdout = tester.get_stdout()
    assert any(
        ["ERROR FROM server: chyba" in line for line in stdout.split("\n")]
    ), "Output does not match expected error message."

    # Should receive CONFIRM for the ERROR message
    message = tester.receive_message()
    assert (
        message == b"\x00\x00\x01"
    ), "Incoming message does not match expected CONFIRM message."

    # Should receive BYE for the ERROR message
    # !!!Based on new FSM BYE is not mandatory anymore!!!
    # message = tester.receive_message()
    # tMessage = translateMessage(message)
    # assert (
    #     tMessage == "BYE FROM c\r\n"
    # ), "Incoming message does not match expected BYE message."


@testcase
def udp_join_ok(tester):
    """Test that the program sends the correct JOIN message and hadles REPLY correctly."""
    auth_and_reply(tester)

    tester.execute("/rename user")

    tester.execute("/join channel")

    # Expect the join message to be received by the server
    message = tester.receive_message()
    tMessage = translateMessage(message)
    assert (
        tMessage == "JOIN IS channel AS user\r\n"
    ), "Incoming message does not match expected JOIN message."

    # Send REPLY message
    tester.send_message(reply(1, True, getMessageId(message), "jojo"))

    sleep(0.2)

    # Check the output, should contain "Action Success: jojo"
    stdout = tester.get_stdout()
    assert any(
        ["Action Success: jojo" in line for line in stdout.split("\n")]
    ), "Output does not match expected 'Action Success: jojo' output."

    # Should receive CONFIRM for the REPLY message
    message = tester.receive_message()
    assert (
        message == b"\x00\x00\x01"
    ), "Incoming message does not match expected CONFIRM message."


@testcase
def udp_join_nok(tester):
    """Test that the program sends the correct JOIN message and handles a NOK reply correctly."""
    auth_and_reply(tester)

    tester.execute("/rename user")

    tester.execute("/join channel")

    # Expect the join message to be received by the server
    message = tester.receive_message()
    tMessage = translateMessage(message)
    assert (
        tMessage == "JOIN IS channel AS user\r\n"
    ), "Incoming message does not match expected JOIN message."

    # Send REPLY message
    tester.send_message(reply(1, False, getMessageId(message), "nene"))

    sleep(0.2)

    # Check the output, should contain "Action Failure: nene"
    stdout = tester.get_stdout()
    assert any(
        ["Action Failure: nene" in line for line in stdout.split("\n")]
    ), "Output does not match expected 'Action Failure: nene' output."

    # Should receive CONFIRM for the REPLY message
    message = tester.receive_message()
    assert (
        message == b"\x00\x00\x01"
    ), "Incoming message does not match expected CONFIRM message."


@testcase
def udp_multiple_auth(tester):
    """Test that the program does not allow multiple AUTH commands."""
    auth_and_reply(tester)

    tester.execute("/auth d e f")

    sleep(0.2)

    # Client should not allow another auth and should output ERR
    stdout = tester.get_stdout()
    assert any(
        ["ERROR: " in line for line in stdout.split("\n")]
    ), "Output does not match expected 'ERROR: ' output."


@testcase
def udp_invalid_msg(tester):
    """Test that the program handles an invalid message correctly."""
    auth_and_reply(tester)

    # Send invalid message
    tester.send_message(b"\x06\x00\x01")

    sleep(0.2)

    # Check the output, should contain "ERROR: "
    stdout = tester.get_stdout()
    assert any(
        ["ERROR: " in line for line in stdout.split("\n")]
    ), "Output does not match expected 'ERROR: ' output."

    # we should firs expect confirm (Packet loss can be detected using mandatory
    # message confirmation with timeouts. Once a message is sent it is required to confirm its Action successful delivery by the other party.
    # The confirmation should be sent immediately after receiving the message, regardless of any potential higher-level processing issues)
    message = tester.receive_message()
    assert (
        message == b"\x00\x00\x01"
    ), "Incoming message does not match expected CONFIRM message."

    # Should receive ERR for the invalid message
    # !!!Based on new FSM BYE is not mandatory anymore!!!
    message = tester.receive_message()
    tMessage = translateMessage(message)
    assert (
        "ERR FROM c IS" in tMessage
    ), "Incoming message does not match expected ERR message."


@testcase
def udp_auth_err(tester):
    """Test that the program handles an ERR message from the server correctly."""
    tester.start_server("udp", 4567)
    tester.setup(args=["-t", "udp", "-s", "localhost", "-p", "4567"])

    # Send AUTH command
    tester.execute("/auth a b c")

    # Expect the auth message to be received by the server
    message = tester.receive_message()
    tMessage = translateMessage(message)
    assert (
        tMessage == "AUTH IS a AS c USING b\r\n"
    ), "Incoming message does not match expected AUTH message."

    # Send ERR message
    tester.send_message(err(1, "server", "ajaj"))

    sleep(0.2)

    # The client should output the ERR message exactly like this
    stdout = tester.get_stdout()
    assert any(
        ["ERROR FROM server: ajaj" in line for line in stdout.split("\n")]
    ), "Output does not match expected error message."

    # Check confirm of the ERR message
    message = tester.receive_message()
    assert (
        message == b"\x00\x00\x01"
    ), "Incoming message does not match expected CONFIRM message."

    # The client should respond with BYE message
    # !!!Based on new FSM BYE is not mandatory anymore!!!
    # message = tester.receive_message()
    # tMessage = translateMessage(message)
    # assert (
    #     tMessage == "BYE FROM c\r\n"
    # ), "Incoming message does not match expected BYE message."


# PART 3: TCP

@testcase
def tcp_sigint(tester):
    """Test that the program handles SIGINT correctly."""
    tcp_auth_and_reply(tester)

    # Send SIGINT signal
    tester.send_signal(signal.SIGINT)

    # Expect BYE message
    message = tester.receive_message()
    assert message == "BYE FROM c\r\n", "Incoming message does not match expected BYE message after SIGINT."

@testcase
def tcp_server_bye(tester):
    """Test that the client terminates correctly upon receiving BYE from the server."""
    tcp_auth_and_reply(tester)
    tester.send_message("BYE FROM server\r\n") # Server sends BYE
    sleep(0.2)
    # Client should terminate
    assert tester.process.poll() is not None, "Client process should terminate after receiving BYE."

@testcase
def tcp_rename(tester):
    """Test renaming and sending a message with the new name."""
    tcp_auth_and_reply(tester) # Initial auth as 'c'
    tester.execute("/rename user1")
    sleep(0.2) 
    tester.execute("message after rename")

    # Expect MSG with the new display name 'user1'
    message = tester.receive_message()
    assert (
        message == "MSG FROM user1 IS message after rename\r\n"
    ), "Message should be sent with the new display name 'user1'."

@testcase
def tcp_rename_multiple(tester):
    """Test renaming multiple times and sending a message."""
    tcp_auth_and_reply(tester) # Initial auth as 'c'
    tester.execute("/rename user1")
    sleep(0.2)
    tester.execute("/rename user2")
    sleep(0.2)
    tester.execute("message after rename")

    # Expect MSG with the latest display name 'user2'
    message = tester.receive_message()
    assert (
        message == "MSG FROM user2 IS message after rename\r\n"
    ), "Message should be sent with the latest display name 'user2'."

@testcase
def tcp_help_command(tester):
    """Test that the /help command shows some output."""
    tcp_auth_and_reply(tester)
    tester.execute("/help")
    sleep(1)
    stdout = tester.get_stdout()
    assert len(stdout.strip()) > 0, "Expected some output for /help command."

@testcase
def tcp_send_receive_multiple(tester):
    """Test sending and receiving multiple messages."""
    tcp_auth_and_reply(tester)
    tester.execute("client msg 1")
    message = tester.receive_message()
    assert message == "MSG FROM c IS client msg 1\r\n", "Expected client msg 1"

    tester.send_message("MSG FROM server IS server msg 1\r\n")
    sleep(0.2)
    stdout = tester.get_stdout()
    assert "server: server msg 1" in stdout, "Expected server msg 1 in output"

    tester.execute("client msg 2")
    message = tester.receive_message()
    assert message == "MSG FROM c IS client msg 2\r\n", "Expected client msg 2"

    tester.send_message("MSG FROM server IS server msg 2\r\n")
    sleep(0.2)
    stdout = tester.get_stdout()
    assert "server: server msg 2" in stdout, "Expected server msg 2 in output"

@testcase
def tcp_multiple_messages_single_segment(tester):
    """Test receiving multiple messages concatenated in a single TCP segment."""
    tcp_auth_and_reply(tester)

    tester.send_message("MSG FROM server1 IS msg1\r\nMSG FROM server2 IS msg2\r\n")
    sleep(1) 

    stdout = tester.get_stdout()
    assert "server1: msg1" in stdout, "Expected first message in output."
    assert "server2: msg2" in stdout, "Expected second message in output."


@testcase
def tcp_single_message_multiple_segments2(tester):
    """Test receiving a single message split across multiple TCP segments."""
    tcp_auth_and_reply(tester)

    tester.send_message("M") 
    sleep(0.2) # Short delay
    tester.send_message("SG ") 
    sleep(0.2) # Short delay
    tester.send_message("FR") 
    sleep(0.2) # Short delay
    tester.send_message("OM ser") 
    sleep(0.2) # Short delay
    tester.send_message("ve") 
    sleep(0.2) # Short delay
    tester.send_message("r I") 
    sleep(0.2) # Short delay
    tester.send_message("S okeyBroChill\r\n") # Send second part

    sleep(0.2)
    stdout = tester.get_stdout()
    assert "server: okeyBroChill" in stdout, "Expected reassembled message in output."
    
def tcp_single_message_multiple_segments(tester):
    """Test receiving a single message split across multiple TCP segments."""
    tcp_auth_and_reply(tester)

    tester.send_message("MSG FROM server IS part1") 
    sleep(0.2) # Short delay
    tester.send_message("part2\r\n") # Send second part

    sleep(0.2)
    stdout = tester.get_stdout()
    assert "server: part1part2" in stdout, "Expected reassembled message in output."
    
@testcase
def tcp_receive_two_messages_within_3_segments(tester):
    tcp_auth_and_reply(tester)

    tester.send_message("MSG FROM teSter ") 
    sleep(0.2) # Short delay
    tester.send_message("S message1\r\nMSG FROM teSte") 
    sleep(0.2) # Short delay
    tester.send_message(" IS message2\r\n") 
    sleep(0.2)
    stdout = tester.get_stdout()
    assert "teSter: message1\nteSte: message2" in stdout, "Expected reassembled message in output."
    
    
    
@testcase
def tcp_hello(tester):
    """Test that the program does not accept any message commands before the user is authenticated."""
    tester.start_server("tcp", 4567)
    tester.setup(args=["-t", "tcp", "-s", "localhost", "-p", "4567"])

    # Invalid command for the START state
    tester.execute("Hello")

    stdout = tester.get_stdout()
    
    assert "ERROR:" in stdout, "Output does not match expected output."


@testcase
def tcp_not_auth(tester):
    """Test that the program does not accept any join commands before the user is authenticated."""
    tester.start_server("tcp", 4567)
    tester.setup(args=["-t", "tcp", "-s", "localhost", "-p", "4567"])
    tester.execute("/join")

    # The ERR message should be printed out exactly like this
    stdout = tester.get_stdout()
    assert any(
        ["ERROR:" in line for line in stdout.split("\n")]
    ), "Output does not match expected error message."


@testcase
def tcp_invalid_command(tester):
    """Test that the program does not accept invalid commands."""
    tester.start_server("tcp", 4567)
    tester.setup(args=["-t", "tcp", "-s", "localhost", "-p", "4567"])

    # Invalid command in general
    tester.execute("/pepe")

    stdout = tester.get_stdout()
    
    assert "ERROR:" in stdout, "Output does not match expected output."


@testcase
def tcp_auth(tester):
    """Test that the program sends the correct AUTH message."""
    tester.start_server("tcp", 4567)
    tester.setup(args=["-t", "tcp", "-s", "localhost", "-p", "4567"])
    tester.execute("/auth a b c")

    message = tester.receive_message()
    assert (
        message == "AUTH a AS c USING b\r\n"
    ), "Incoming message does not match expected AUTH message."


@testcase
def tcp_auth_ok(tester):
    """Test that the program handles a Action successful reply to AUTH correctly."""
    tester.start_server("tcp", 4567)
    tester.setup(args=["-t", "tcp", "-s", "localhost", "-p", "4567"])
    tester.execute("/auth a b c")

    # Check AUTH message received
    message = tester.receive_message()
    assert (
        message == "AUTH a AS c USING b\r\n"
    ), "Incoming message does not match expected AUTH message."

    # Send REPLY message
    tester.send_message("REPLY OK IS vsechno cajk\r\n")

    sleep(0.2)

    # Check the output, should contain "Action Success: vsechno cajk"
    stdout = tester.get_stdout()
    assert any(
        ["Action Success: vsechno cajk" == line for line in stdout.split("\n")]
    ), "Output does not match expected error message."


@testcase
def tcp_auth_nok(tester):
    """Test that the program handles a NOK reply to AUTH correctly."""
    tester.start_server("tcp", 4567)
    tester.setup(args=["-t", "tcp", "-s", "localhost", "-p", "4567"])
    tester.execute("/auth a b c")

    message = tester.receive_message()
    assert (
        message == "AUTH a AS c USING b\r\n"
    ), "Incoming message does not match expected AUTH message."

    tester.send_message("REPLY NOK IS nic cajk\r\n")

    sleep(0.2)

    stdout = tester.get_stdout()
    assert any(
        ["Action Failure: nic cajk" == line for line in stdout.split("\n")]
    ), "Output does not match expected error message."


@testcase
def tcp_auth_port(tester):
    """Test that the program sends the correct AUTH message when the port is non-default."""
    tester.start_server("tcp", 4321)
    tester.setup(args=["-t", "tcp", "-s", "localhost", "-p", "4321"])

    # Send AUTH command
    tester.execute("/auth a b c")

    # Check AUTH message received
    message = tester.receive_message()
    assert (
        message == "AUTH a AS c USING b\r\n"
    ), "Incoming message does not match expected message."


@testcase
def tcp_auth_nok_ok(tester):
    """Test that the program handles a NOK reply to AUTH followed by a Action successful AUTH correctly."""
    tester.start_server("tcp", 4567)
    tester.setup(args=["-t", "tcp", "-s", "localhost", "-p", "4567"])
    tester.execute("/auth a b c")

    message = tester.receive_message()
    assert (
        message == "AUTH a AS c USING b\r\n"
    ), "Incoming message does not match expected AUTH message."

    tester.send_message("REPLY NOK IS nic cajk\r\n")

    sleep(0.2)

    stdout = tester.get_stdout()
    assert any(
        ["Action Failure: nic cajk" == line for line in stdout.split("\n")]
    ), "Output does not match expected error message."

    tester.execute("/auth d e f")

    message = tester.receive_message()
    assert (
        message == "AUTH d AS f USING e\r\n"
    ), "Incoming message does not match expected AUTH message."

    tester.send_message("REPLY OK IS vsechno cajk\r\n")

    sleep(0.2)

    stdout = tester.get_stdout()
    assert any(
        ["Action Success: vsechno cajk" == line for line in stdout.split("\n")]
    ), "Output does not match expected error message."


# Helper function
def tcp_auth_and_reply(tester):
    """Helper function to test the AUTH command followed by a Action successful reply."""
    tester.start_server("tcp", 4567)
    tester.setup(args=["-t", "tcp", "-s", "localhost", "-p", "4567"])

    # Send AUTH command
    tester.execute("/auth a b c")

    # Expect the auth message to be received by the server
    message = tester.receive_message()
    assert (
        message == "AUTH a AS c USING b\r\n"
    ), "Incoming message does not match expected AUTH message."

    # Send REPLY message
    tester.send_message("REPLY OK IS vsechno cajk\r\n")

    sleep(0.2)

    # Check the output, should contain "Action Success: vsechno cajk"
    stdout = tester.get_stdout()
    assert any(
        ["Action Success: vsechno cajk" == line for line in stdout.split("\n")]
    ), "Output does not match expected error message."


@testcase
def tcp_auth_ok(tester):
    """Test that the program handles a Action successful reply to AUTH correctly."""
    tcp_auth_and_reply(tester)


@testcase
def tcp_msg(tester):
    """Test that the program sends the correct MSG message."""
    tcp_auth_and_reply(tester)

    tester.execute("ahojky")

    # Expect the message to be received by the server
    message = tester.receive_message()
    assert (
        message == "MSG FROM c IS ahojky\r\n"
    ), "Incoming message does not match expected MSG message."


@testcase
def tcp_svr_msg(tester):
    """Test that the program handles a MSG message from the server correctly."""
    tcp_auth_and_reply(tester)

    # Send a message from the server
    tester.send_message("MSG FROM SeverusSnape IS ahojky\r\n")

    sleep(0.2)

    # Check the output, should contain "ahojky"
    stdout = tester.get_stdout()
    assert any(
        ["SeverusSnape: ahojky" in line for line in stdout.split("\n")]
    ), "Output does not match expected output."


@testcase
def tcp_bye(tester):
    """Test that the program handles a C-d (stdin closed) correctly."""
    tcp_auth_and_reply(tester)

    # Send a message from the server
    tester.process.stdin.close()

    message = tester.receive_message()
    assert message == "BYE FROM c\r\n", "Incoming message does not match expected BYE message."


@testcase
def tcp_server_err1(tester):
    """Test that the program handles an ERR message from the server correctly while waiting for REPLY."""
    tester.start_server("tcp", 4567)
    tester.setup(args=["-t", "tcp", "-s", "localhost", "-p", "4567"])

    # Execute AUTH command
    tester.execute("/auth a b c")

    # Send a message from the server
    tester.send_message("ERR FROM jetovp IS rdeli\r\n")

    sleep(0.4)

    stdout = tester.get_stdout()
    assert any(
        ["ERROR FROM jetovp: rdeli" in line for line in stdout.split("\n")]
    ), "Output does not match expected error message."


@testcase
def tcp_server_err2(tester):
    """Test that the program handles an ERR message from the server correctly."""
    tcp_auth_and_reply(tester)

    # Send a message from the server
    
    tester.send_message("ERR FROM chuj IS bobr\r\n")

    sleep(0.4)

    stdout = tester.get_stdout()
    assert any(
        ["ERROR FROM chuj: bobr" in line for line in stdout.split("\n")]
    ), "Output does not match expected error message."


@testcase
def tcp_join_ok(tester):
    """Test that the program sends the correct JOIN message and handles REPLY correctly."""
    tcp_auth_and_reply(tester)

    tester.execute("/rename user")

    tester.execute("/join channel")

    # Expect the join message to be received by the server
    message = tester.receive_message()
    assert (
        message == "JOIN channel AS user\r\n"
    ), "Incoming message does not match expected JOIN message."

    # Send REPLY message
    tester.send_message("REPLY OK IS takjo brasko ale bud hodnej\r\n")

    sleep(0.2)

    # Check the output, should contain "Action Success: jojo"
    stdout = tester.get_stdout()
    assert any(
        ["Action Success: takjo brasko ale bud hodnej" in line for line in stdout.split("\n")]
    ), "Output does not match expected 'Action Success: jojo' output."


@testcase
def tcp_join_nok(tester):
    """Test that the program sends the correct JOIN message and handles a NOK reply correctly."""
    tcp_auth_and_reply(tester)

    tester.execute("/rename user")

    tester.execute("/join channel")

    # Expect the join message to be received by the server
    message = tester.receive_message()
    assert (
        message == "JOIN channel AS user\r\n"
    ), "Incoming message does not match expected JOIN message."

    # Send REPLY message
    tester.send_message("REPLY NOK IS minus boiiii\r\n")

    sleep(0.2)

    # Check the output, should contain "Action Success: jojo"
    stdout = tester.get_stdout()
    assert any(
        ["Action Failure: minus boiiii" in line for line in stdout.split("\n")]
    ), "Output does not match expected 'Action Success: jojo' output."


@testcase
def tcp_multiple_auth(tester):
    """Test that the program does not allow multiple AUTH commands."""
    tcp_auth_and_reply(tester)

    tester.execute("/auth d e f")

    sleep(0.2)

    # Client should not allow another auth and should output ERR
    stdout = tester.get_stdout()
    assert any(
        ["ERROR: " in line for line in stdout.split("\n")]
    ), "Output does not match expected 'ERROR: ' output."


@testcase
def tcp_invalid_msg(tester):
    """Test that the program handles an invalid message correctly and responds appropriately."""

    tcp_auth_and_reply(tester)

    # Send an invalid message
    tester.send_message("TVOJE MAMINKA\r\n")

    sleep(0.2)

    # Check stdout contains an appropriate error message
    stdout = tester.get_stdout()
    assert any(
        "ERROR: " in line for line in stdout.split("\n")
    ), "Output does not contain expected 'ERROR: ' message."

    # Collect responses (messages may be split or combined)
    received = ""
    for _ in range(2):  # Try to receive up to 2 packets
        try:
            part = tester.receive_message(timeout=0.5)
            if part:
                received += part
        except TimeoutError:
            break


    # Use regex to validate both messages are present
    # !!!Based on new FSM BYE is not mandatory anymore!!!
    err_match = re.search(r"ERR FROM c IS [ -~]+\r\n", received)
    # bye_match = re.search(r"BYE FROM c\r\n", received)

    assert err_match, "Missing or malformed ERR message."
    # assert bye_match, "Missing BYE message."

    # Close client's stdin
    tester.process.stdin.close()
    sleep(0.2)

@testcase
def tcp_auth_err(tester):
    """Test that the program handles an ERR message from the server correctly."""
    tester.start_server("tcp", 4567)
    tester.setup(args=["-t", "tcp", "-s", "localhost", "-p", "4567"])
    tester.execute("/auth a b c")

    # Expect the auth message to be received by the server
    message = tester.receive_message()
    assert (
        message == "AUTH a AS c USING b\r\n"
    ), "Incoming message does not match expected AUTH message."

    # Send ERR message
    tester.send_message("ERR FROM server IS ajaj\r\n")
    sleep(0.2)

    # The client should output the ERR message exactly like this
    stdout = tester.get_stdout()
    assert any(
        ["ERROR FROM server: ajaj" in line for line in stdout.split("\n")]
    ), "Output does not match expected error message."

    # The client should respond with BYE message
    # !!!Based on new FSM BYE is not mandatory anymore!!!
    # message = tester.receive_message()
    # assert message == "BYE FROM c\r\n", "Incoming message does not match expected BYE message."


### END TEST CASES ###


def run_tests(executable_path, udp=False, tcp=False, test_case=None):
    test_cases_passed = 0
    tester = ExecutableTester(executable_path)

    total_cases = 0

    if test_case is None:
        # Test UDP
        if udp:
            total_cases += len(udp_test_cases)
            for test in udp_test_cases:
                tester = ExecutableTester(executable_path)
                test_cases_passed += 1 if test(tester) else 0

        # Test TCP
        if tcp:
            total_cases += len(tcp_test_cases)
            for test in tcp_test_cases:
                tester = ExecutableTester(executable_path)
                test_cases_passed += 1 if test(tester) else 0

    else:
        test_found = False

        # Find and run the specified UDP test case
        if udp:
            for udp_test in udp_test_cases:
                if udp_test.__name__ == test_case:
                    total_cases += 1
                    test_cases_passed += 1 if udp_test(tester) else 0
                    test_found = True
                    break

        # Find and run the specified TCP test case if it wasn't found in UDP
        if tcp and not test_found:
            for tcp_test in tcp_test_cases:
                if tcp_test.__name__ == test_case:
                    total_cases += 1
                    test_cases_passed += 1 if tcp_test(tester) else 0
                    test_found = True
                    break

        if not test_found:
            print(
                f"Error: Test case '{test_case}' not found in the specified protocol tests."
            )
            return

    cprint(
        f"\n{'✅' if test_cases_passed == total_cases else '❌'} {test_cases_passed}/{total_cases} test cases passed",
        "green" if test_cases_passed == total_cases else "red",
    )


if __name__ == "__main__":
    # Default settings
    debug = False
    udp = True
    tcp = True
    test_case = None

    executable_path = None

    if "-h" in sys.argv:
        print(
            "Usage: python test_executable.py <path_to_executable> [-d: debug] [-t <testcase_name>] [-p <udp|tcp>]"
        )
        sys.exit(0)

    args = iter(sys.argv[1:])  # Skip the script name
    for arg in args:
        if arg == "-d":
            debug = True
        elif arg == "-t":
            try:
                test_case = next(args)  # Get the next argument as the test case name
                if test_case not in [
                    tc.__name__ for tc in udp_test_cases
                ] and test_case not in [tc.__name__ for tc in tcp_test_cases]:
                    print(f"Error: Test case '{test_case}' not recognized.")
                    sys.exit(1)
            except StopIteration:
                print("Error: -t requires a test case name.")
                sys.exit(1)
        elif arg == "-p":
            try:
                protocol = next(args).lower()  # Get the next argument as the protocol
                if protocol == "udp":
                    tcp = False
                elif protocol == "tcp":
                    udp = False
                else:
                    print("Error: Protocol must be either 'udp' or 'tcp'.")
                    sys.exit(1)
            except StopIteration:
                print("Error: -p requires a protocol name (udp or tcp).")
                sys.exit(1)
        else:
            if executable_path is None:
                executable_path = arg
            else:
                print("Error: Unexpected argument or executable path already set.")
                sys.exit(1)

    if executable_path is None:
        print("Error: Path to executable is required.")
        sys.exit(1)

    # Now call run_tests with the collected options
    run_tests(executable_path, udp=udp, tcp=tcp, test_case=test_case)
