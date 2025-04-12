MESSAGE_TYPES = {
    0x00: "CONFIRM",
    0x01: "REPLY",
    0x02: "AUTH",
    0x03: "JOIN",
    0x04: "MSG",
    0xFE: "ERR",
    0xFF: "BYE",
}


def getMessageId(data):
    pointer = 1  # Start after the message type byte
    message_id, pointer = read_bytes(data, pointer, 2)
    return message_id


def read_byte(data, pointer):
    byte = data[pointer]
    pointer += 1
    return byte, pointer


def read_bytes(data, pointer, num_bytes):
    bytes_read = data[pointer : pointer + num_bytes]
    pointer += num_bytes
    return bytes_read, pointer


def read_variable_length_string(data, pointer):
    string = b""
    while True:
        byte, pointer = read_byte(data, pointer)
        if byte == 0:
            break
        string += bytes([byte])
    return string.decode(), pointer


def translateMessage(data):
    pointer = 0
    message_type, pointer = read_byte(data, pointer)
    if message_type not in MESSAGE_TYPES:
        return "Unknown message type"

    message_id, pointer = read_bytes(data, pointer, 2)

    if message_type == 0x00:  # CONFIRM
        ref_message_id, pointer = read_bytes(data, pointer, 2)
        return f'REPLY IS {int.from_bytes(ref_message_id, byteorder="big")}\r\n'

    elif message_type == 0x01:  # REPLY
        result, pointer = read_byte(data, pointer)
        ref_message_id, pointer = read_bytes(data, pointer, 2)
        message_contents, pointer = read_variable_length_string(data, pointer)
        return f'REPLY {"OK" if result == 1 else "NOK"} IS {int.from_bytes(ref_message_id, byteorder="big")} AS {message_contents}\r\n'

    elif message_type == 0x02:  # AUTH
        username, pointer = read_variable_length_string(data, pointer)
        display_name, pointer = read_variable_length_string(data, pointer)
        secret, pointer = read_variable_length_string(data, pointer)
        return f"AUTH IS {username} AS {display_name} USING {secret}\r\n"

    elif message_type == 0x03:  # JOIN
        channel_id, pointer = read_variable_length_string(data, pointer)
        display_name, pointer = read_variable_length_string(data, pointer)
        return f"JOIN IS {channel_id} AS {display_name}\r\n"

    elif message_type == 0x04:  # MSG
        display_name, pointer = read_variable_length_string(data, pointer)
        message_contents, pointer = read_variable_length_string(data, pointer)
        return f"MSG FROM {display_name} IS {message_contents}\r\n"

    elif message_type == 0xFE:  # ERR
        display_name, pointer = read_variable_length_string(data, pointer)
        message_contents, pointer = read_variable_length_string(data, pointer)
        return f"ERR FROM {display_name} IS {message_contents}\r\n"

    elif message_type == 0xFF:  # BYE
        display_name, pointer = read_variable_length_string(data, pointer)
        return f"BYE FROM {display_name}\r\n"
