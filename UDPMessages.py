def confirm(ref_message_id):
    return bytes([0x00]) + ref_message_id

def reply(message_id, result, ref_message_id, message_contents):
    message_id_bytes = message_id.to_bytes(2, 'big')
    result_byte = bytes([result])
    message_contents_bytes = message_contents.encode() + b'\x00'
    return bytes([0x01]) + message_id_bytes + result_byte + ref_message_id + message_contents_bytes

def auth(message_id, username, display_name, secret):
    message_id_bytes = message_id.to_bytes(2, 'big')
    username_bytes = username.encode() + b'\x00'
    display_name_bytes = display_name.encode() + b'\x00'
    secret_bytes = secret.encode() + b'\x00'
    return bytes([0x02]) + message_id_bytes + username_bytes + display_name_bytes + secret_bytes

def join(message_id, channel_id, display_name):
    message_id_bytes = message_id.to_bytes(2, 'big')
    channel_id_bytes = channel_id.encode() + b'\x00'
    display_name_bytes = display_name.encode() + b'\x00'
    return bytes([0x03]) + message_id_bytes + channel_id_bytes + display_name_bytes

def msg(message_id, display_name, message_contents):
    message_id_bytes = message_id.to_bytes(2, 'big')
    display_name_bytes = display_name.encode() + b'\x00'
    message_contents_bytes = message_contents.encode() + b'\x00'
    return bytes([0x04]) + message_id_bytes + display_name_bytes + message_contents_bytes

def err(message_id, display_name, message_contents):
    message_id_bytes = message_id.to_bytes(2, 'big')
    display_name_bytes = display_name.encode() + b'\x00'
    message_contents_bytes = message_contents.encode() + b'\x00'
    return bytes([0xFE]) + message_id_bytes + display_name_bytes + message_contents_bytes

def bye(message_id):
    return bytes([0xFF]) + message_id.to_bytes(2, 'big')

def ping(message_id):
    return bytes([0xFD]) + message_id.to_bytes(2, 'big')