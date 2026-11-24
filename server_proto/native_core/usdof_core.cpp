#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <limits>
#include <utility>
#include <vector>

namespace {

constexpr int kAbiVersion = 10;
constexpr const char* kBuildVersion = "phase2-p9-codec13";

int character_flow_operation_id(uint32_t opcode) {
    switch (opcode) {
    case 0x0004:
        return 1;  // SELECT_CHARACTER / enter Seria
    case 0x0005:
        return 2;  // CREATE_CHARACTER
    case 0x0006:
        return 3;  // DELETE_CHARACTER
    case 0x0007:
        return 4;  // RETURN_SELECT_CHARACTER
    case 0x0008:
        return 5;  // GET_USERINFO / character-page refresh
    case 0x0127:
        return 6;  // CHANGE_CHARAC_SLOT
    case 0x02B5:
        return 7;  // CHECK_CHARACTER_NAME
    case 0x02C1:
        return 8;  // CHARACTER_CARD_CLICK
    case 0x02A8:
        return 9;  // LOAD_EXTEND_CHARACS
    case 0x01BA:
        return 10;  // LOAD_EXTEND_CHARACS_FINISH
    case 0x0355:
        return 11;  // EXTEND_READY / create refresh
    case 0x057F:
        return 12;  // CHANNEL_MOVE_SELECTING_SCENE
    default:
        return 0;
    }
}

class BufferViews {
public:
    explicit BufferViews(Py_ssize_t count)
        : views_(static_cast<size_t>(count)), acquired_(0) {}

    ~BufferViews() {
        release();
    }

    BufferViews(const BufferViews&) = delete;
    BufferViews& operator=(const BufferViews&) = delete;

    bool acquire(PyObject* sequence) {
        const Py_ssize_t count = PySequence_Fast_GET_SIZE(sequence);
        PyObject** items = PySequence_Fast_ITEMS(sequence);
        for (Py_ssize_t index = 0; index < count; ++index) {
            Py_buffer& view = views_[static_cast<size_t>(index)];
            if (PyObject_GetBuffer(items[index], &view, PyBUF_CONTIG_RO) != 0) {
                return false;
            }
            ++acquired_;
        }
        return true;
    }

    const Py_buffer& operator[](size_t index) const {
        return views_[index];
    }

    size_t size() const {
        return views_.size();
    }

private:
    void release() {
        for (size_t index = 0; index < acquired_; ++index) {
            PyBuffer_Release(&views_[index]);
        }
        acquired_ = 0;
    }

    std::vector<Py_buffer> views_;
    size_t acquired_;
};

void store_u32_le(uint8_t* destination, uint32_t value) {
    destination[0] = static_cast<uint8_t>(value);
    destination[1] = static_cast<uint8_t>(value >> 8);
    destination[2] = static_cast<uint8_t>(value >> 16);
    destination[3] = static_cast<uint8_t>(value >> 24);
}

void store_u16_le(uint8_t* destination, uint16_t value) {
    destination[0] = static_cast<uint8_t>(value);
    destination[1] = static_cast<uint8_t>(value >> 8);
}

uint32_t load_u32_le(const uint8_t* source) {
    return static_cast<uint32_t>(source[0]) |
           (static_cast<uint32_t>(source[1]) << 8) |
           (static_cast<uint32_t>(source[2]) << 16) |
           (static_cast<uint32_t>(source[3]) << 24);
}

uint16_t load_u16_le(const uint8_t* source) {
    return static_cast<uint16_t>(source[0]) |
           (static_cast<uint16_t>(source[1]) << 8);
}

bool read_uint_field(
    PyObject* value,
    unsigned long long maximum,
    const char* field_name,
    unsigned long long* output
) {
    if (!PyLong_Check(value)) {
        PyErr_Format(PyExc_TypeError, "%s must be an integer", field_name);
        return false;
    }
    const unsigned long long parsed = PyLong_AsUnsignedLongLong(value);
    if (PyErr_Occurred()) {
        return false;
    }
    if (parsed > maximum) {
        PyErr_Format(PyExc_OverflowError, "%s is outside its wire range", field_name);
        return false;
    }
    *output = parsed;
    return true;
}

uint32_t rol32(uint32_t value, unsigned int shift) {
    shift &= 31u;
    if (shift == 0) {
        return value;
    }
    return static_cast<uint32_t>((value << shift) | (value >> (32u - shift)));
}

uint32_t ror32(uint32_t value, unsigned int shift) {
    shift &= 31u;
    if (shift == 0) {
        return value;
    }
    return static_cast<uint32_t>((value >> shift) | (value << (32u - shift)));
}

void codec13_encode_block(
    uint32_t rounds,
    const uint32_t* schedule,
    const uint8_t* source,
    uint8_t* destination
) {
    uint32_t left = (static_cast<uint32_t>(source[0]) << 24) |
                    (static_cast<uint32_t>(source[1]) << 16) |
                    (static_cast<uint32_t>(source[2]) << 8) |
                    static_cast<uint32_t>(source[3]);
    uint32_t right = (static_cast<uint32_t>(source[4]) << 24) |
                     (static_cast<uint32_t>(source[5]) << 16) |
                     (static_cast<uint32_t>(source[6]) << 8) |
                     static_cast<uint32_t>(source[7]);

    right ^= left;
    unsigned int index = 0;
    unsigned int step = 1;
    unsigned int index_shadow = index;
    if (rounds != step) {
        while (true) {
            uint32_t value = schedule[index] + right;
            ++step;
            uint32_t mixed = value + rol32(value, 1) - 1u;
            left = rol32(mixed, 4) ^ left ^ mixed;
            if (step == rounds) {
                break;
            }

            value = schedule[index + 1] + left;
            mixed = value + rol32(value, 2) + 1u;
            value = rol32(mixed, 8) ^ mixed;
            value += schedule[index + 2];
            mixed = rol32(value, 1) - value;
            value = rol32(mixed, 16) ^ (left | mixed);
            right ^= value;
            ++step;
            if (step == rounds) {
                break;
            }

            index = index_shadow;
            value = schedule[index + 3] + right;
            value = rol32(value, 2) + value + 1u;
            left = value ^ left;
            ++step;
            if (step == rounds) {
                break;
            }

            right ^= left;
            index ^= 4u;
            ++step;
            index_shadow = index;
            if (step == rounds) {
                break;
            }
        }
    }

    const uint32_t out_left = left;
    const uint32_t out_right = right;
    destination[0] = static_cast<uint8_t>(out_left >> 24);
    destination[1] = static_cast<uint8_t>(out_left >> 16);
    destination[2] = static_cast<uint8_t>(out_left >> 8);
    destination[3] = static_cast<uint8_t>(out_left);
    destination[4] = static_cast<uint8_t>(out_right >> 24);
    destination[5] = static_cast<uint8_t>(out_right >> 16);
    destination[6] = static_cast<uint8_t>(out_right >> 8);
    destination[7] = static_cast<uint8_t>(out_right);
}

void codec13_decode_block(
    uint32_t rounds,
    const uint32_t* schedule,
    const uint8_t* source,
    uint8_t* destination
) {
    uint32_t left = (static_cast<uint32_t>(source[0]) << 24) |
                    (static_cast<uint32_t>(source[1]) << 16) |
                    (static_cast<uint32_t>(source[2]) << 8) |
                    static_cast<uint32_t>(source[3]);
    uint32_t right = (static_cast<uint32_t>(source[4]) << 24) |
                     (static_cast<uint32_t>(source[5]) << 16) |
                     (static_cast<uint32_t>(source[6]) << 8) |
                     static_cast<uint32_t>(source[7]);
    uint32_t remaining = rounds;
    unsigned int index = ((~remaining) & 1u) * 4u;
    while (remaining >= 4u) {
        uint32_t value = schedule[index + 3] + right;
        value = rol32(value, 2) + value + 1u;
        left ^= value;
        --remaining;

        value = schedule[index + 1] + left;
        uint32_t mixed = value + rol32(value, 2) + 1u;
        value = rol32(mixed, 8) ^ mixed;
        value += schedule[index + 2];
        mixed = rol32(value, 1) - value;
        value = rol32(mixed, 16) ^ (left | mixed);
        right ^= value;
        --remaining;

        value = right + schedule[index];
        value = value + rol32(value, 1) - 1u;
        left = rol32(value, 4) ^ left ^ value;
        --remaining;

        right ^= left;
        --remaining;
        index ^= 4u;
    }

    destination[0] = static_cast<uint8_t>(left >> 24);
    destination[1] = static_cast<uint8_t>(left >> 16);
    destination[2] = static_cast<uint8_t>(left >> 8);
    destination[3] = static_cast<uint8_t>(left);
    destination[4] = static_cast<uint8_t>(right >> 24);
    destination[5] = static_cast<uint8_t>(right >> 16);
    destination[6] = static_cast<uint8_t>(right >> 8);
    destination[7] = static_cast<uint8_t>(right);
}

bool codec13_schedule_from_key(
    const uint8_t* key,
    uint32_t* rounds_out,
    uint32_t* schedule
) {
    uint32_t words[10] = {};
    for (int offset = 0; offset < 10; ++offset) {
        const size_t base = static_cast<size_t>(offset) * 4u;
        words[offset] = (static_cast<uint32_t>(key[base]) << 24) |
                        (static_cast<uint32_t>(key[base + 1]) << 16) |
                        (static_cast<uint32_t>(key[base + 2]) << 8) |
                        static_cast<uint32_t>(key[base + 3]);
    }
    const uint32_t src[8] = {
        words[0], words[1], words[2], words[3],
        words[4], words[5], words[6], words[7],
    };
    uint32_t left = words[8];
    uint32_t right = words[9] ^ left;

    uint32_t value = src[0] + right;
    value = value + rol32(value, 1) - 1u;
    left ^= rol32(value, 4) ^ value;
    schedule[0] = left;

    value = src[1] + left;
    value = value + rol32(value, 2) + 1u;
    uint32_t mixed = rol32(value, 8) ^ value;
    mixed += src[2];
    value = rol32(mixed, 1) - mixed;
    mixed = rol32(value, 16) ^ (value | left);
    right ^= mixed;
    schedule[1] = right;

    value = src[3] + right;
    value = rol32(value, 2) + value + 1u;
    left ^= value;
    right ^= left;
    schedule[2] = left;
    schedule[3] = right;

    value = src[4] + right;
    value = value + rol32(value, 1) - 1u;
    left ^= rol32(value, 4) ^ value;
    schedule[4] = left;

    value = src[5] + left;
    value = value + rol32(value, 2) + 1u;
    mixed = rol32(value, 8) ^ value;
    mixed += src[6];
    value = rol32(mixed, 1) - mixed;
    mixed = rol32(value, 16) ^ (value | left);
    right ^= mixed;
    schedule[5] = right;

    value = src[7] + right;
    value = rol32(value, 2) + value + 1u;
    left ^= value;
    right ^= left;
    schedule[6] = left;
    schedule[7] = right;
    *rounds_out = 0x80u;
    return true;
}

constexpr uint8_t kAesSbox[256] = {
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
};

constexpr uint8_t kAesInvSbox[256] = {
    0x52, 0x09, 0x6a, 0xd5, 0x30, 0x36, 0xa5, 0x38, 0xbf, 0x40, 0xa3, 0x9e, 0x81, 0xf3, 0xd7, 0xfb,
    0x7c, 0xe3, 0x39, 0x82, 0x9b, 0x2f, 0xff, 0x87, 0x34, 0x8e, 0x43, 0x44, 0xc4, 0xde, 0xe9, 0xcb, 0x54,
    0x7b, 0x94, 0x32, 0xa6, 0xc2, 0x23, 0x3d, 0xee, 0x4c, 0x95, 0x0b, 0x42, 0xfa, 0xc3, 0x4e, 0x08, 0x2e,
    0xa1, 0x66, 0x28, 0xd9, 0x24, 0xb2, 0x76, 0x5b, 0xa2, 0x49, 0x6d, 0x8b, 0xd1, 0x25, 0x72, 0xf8,
    0xf6, 0x64, 0x86, 0x68, 0x98, 0x16, 0xd4, 0xa4, 0x5c, 0xcc, 0x5d, 0x65, 0xb6, 0x92, 0x6c, 0x70,
    0x48, 0x50, 0xfd, 0xed, 0xb9, 0xda, 0x5e, 0x15, 0x46, 0x57, 0xa7, 0x8d, 0x9d, 0x84, 0x90, 0xd8,
    0xab, 0x00, 0x8c, 0xbc, 0xd3, 0x0a, 0xf7, 0xe4, 0x58, 0x05, 0xb8, 0xb3, 0x45, 0x06, 0xd0, 0x2c,
    0x1e, 0x8f, 0xca, 0x3f, 0x0f, 0x02, 0xc1, 0xaf, 0xbd, 0x03, 0x01, 0x13, 0x8a, 0x6b, 0x3a, 0x91,
    0x11, 0x41, 0x4f, 0x67, 0xdc, 0xea, 0x97, 0xf2, 0xcf, 0xce, 0xf0, 0xb4, 0xe6, 0x73, 0x96, 0xac,
    0x74, 0x22, 0xe7, 0xad, 0x35, 0x85, 0xe2, 0xf9, 0x37, 0xe8, 0x1c, 0x75, 0xdf, 0x6e, 0x47, 0xf1,
    0x1a, 0x71, 0x1d, 0x29, 0xc5, 0x89, 0x6f, 0xb7, 0x62, 0x0e, 0xaa, 0x18, 0xbe, 0x1b, 0xfc, 0x56,
    0x3e, 0x4b, 0xc6, 0xd2, 0x79, 0x20, 0x9a, 0xdb, 0xc0, 0xfe, 0x78, 0xcd, 0x5a, 0xf4, 0x1f, 0xdd,
    0xa8, 0x33, 0x88, 0x07, 0xc7, 0x31, 0xb1, 0x12, 0x10, 0x59, 0x27, 0x80, 0xec, 0x5f, 0x60, 0x51,
    0x7f, 0xa9, 0x19, 0xb5, 0x4a, 0x0d, 0x2d, 0xe5, 0x7a, 0x9f, 0x93, 0xc9, 0x9c, 0xef, 0xa0, 0xe0,
    0x3b, 0x4d, 0xae, 0x2a, 0xf5, 0xb0, 0xc8, 0xeb, 0xbb, 0x3c, 0x83, 0x53, 0x99, 0x61, 0x17, 0x2b,
    0x04, 0x7e, 0xba, 0x77, 0xd6, 0x26, 0xe1, 0x69, 0x14, 0x63, 0x55, 0x21, 0x0c, 0x7d,
};

uint8_t aes_xtime(uint8_t value) {
    return static_cast<uint8_t>((value << 1) ^ ((value & 0x80u) ? 0x1Bu : 0u));
}

uint8_t aes_gf_mul(uint8_t a, uint8_t b) {
    uint8_t output = 0;
    for (int index = 0; index < 8; ++index) {
        if (b & 1u) {
            output = static_cast<uint8_t>(output ^ a);
        }
        a = aes_xtime(a);
        b = static_cast<uint8_t>(b >> 1);
    }
    return output;
}

void aes128_expand_key(const uint8_t* key, uint8_t* round_keys) {
    std::memcpy(round_keys, key, 16);
    uint8_t rcon = 1;
    size_t generated = 16;
    while (generated < 176) {
        uint8_t temp[4] = {
            round_keys[generated - 4],
            round_keys[generated - 3],
            round_keys[generated - 2],
            round_keys[generated - 1],
        };
        if ((generated % 16) == 0) {
            const uint8_t rotated = temp[0];
            temp[0] = kAesSbox[temp[1]];
            temp[1] = kAesSbox[temp[2]];
            temp[2] = kAesSbox[temp[3]];
            temp[3] = kAesSbox[rotated];
            temp[0] = static_cast<uint8_t>(temp[0] ^ rcon);
            rcon = aes_xtime(rcon);
        }
        for (size_t index = 0; index < 4; ++index) {
            round_keys[generated] = static_cast<uint8_t>(round_keys[generated - 16] ^ temp[index]);
            ++generated;
        }
    }
}

void aes_add_round_key(uint8_t* state, const uint8_t* round_key) {
    for (size_t index = 0; index < 16; ++index) {
        state[index] = static_cast<uint8_t>(state[index] ^ round_key[index]);
    }
}

void aes_sub_bytes(uint8_t* state, const uint8_t* box) {
    for (size_t index = 0; index < 16; ++index) {
        state[index] = box[state[index]];
    }
}

void aes_shift_rows(uint8_t* state, bool inverse) {
    uint8_t original[16];
    std::memcpy(original, state, sizeof(original));
    for (size_t row = 0; row < 4; ++row) {
        for (size_t column = 0; column < 4; ++column) {
            const size_t source_column = inverse
                ? (column + 4 - row) % 4
                : (column + row) % 4;
            state[row + 4 * column] = original[row + 4 * source_column];
        }
    }
}

void aes_mix_columns(uint8_t* state, bool inverse) {
    for (size_t column = 0; column < 4; ++column) {
        const size_t offset = 4 * column;
        const uint8_t a0 = state[offset + 0];
        const uint8_t a1 = state[offset + 1];
        const uint8_t a2 = state[offset + 2];
        const uint8_t a3 = state[offset + 3];
        if (!inverse) {
            state[offset + 0] = static_cast<uint8_t>(aes_gf_mul(a0, 2) ^ aes_gf_mul(a1, 3) ^ a2 ^ a3);
            state[offset + 1] = static_cast<uint8_t>(a0 ^ aes_gf_mul(a1, 2) ^ aes_gf_mul(a2, 3) ^ a3);
            state[offset + 2] = static_cast<uint8_t>(a0 ^ a1 ^ aes_gf_mul(a2, 2) ^ aes_gf_mul(a3, 3));
            state[offset + 3] = static_cast<uint8_t>(aes_gf_mul(a0, 3) ^ a1 ^ a2 ^ aes_gf_mul(a3, 2));
        } else {
            state[offset + 0] = static_cast<uint8_t>(aes_gf_mul(a0, 14) ^ aes_gf_mul(a1, 11) ^ aes_gf_mul(a2, 13) ^ aes_gf_mul(a3, 9));
            state[offset + 1] = static_cast<uint8_t>(aes_gf_mul(a0, 9) ^ aes_gf_mul(a1, 14) ^ aes_gf_mul(a2, 11) ^ aes_gf_mul(a3, 13));
            state[offset + 2] = static_cast<uint8_t>(aes_gf_mul(a0, 13) ^ aes_gf_mul(a1, 9) ^ aes_gf_mul(a2, 14) ^ aes_gf_mul(a3, 11));
            state[offset + 3] = static_cast<uint8_t>(aes_gf_mul(a0, 11) ^ aes_gf_mul(a1, 13) ^ aes_gf_mul(a2, 9) ^ aes_gf_mul(a3, 14));
        }
    }
}

void aes128_encrypt_block(uint8_t* state, const uint8_t* round_keys) {
    aes_add_round_key(state, round_keys);
    for (size_t round = 1; round < 10; ++round) {
        aes_sub_bytes(state, kAesSbox);
        aes_shift_rows(state, false);
        aes_mix_columns(state, false);
        aes_add_round_key(state, round_keys + 16 * round);
    }
    aes_sub_bytes(state, kAesSbox);
    aes_shift_rows(state, false);
    aes_add_round_key(state, round_keys + 160);
}

void aes128_decrypt_block(uint8_t* state, const uint8_t* round_keys) {
    aes_add_round_key(state, round_keys + 160);
    for (int round = 9; round > 0; --round) {
        aes_shift_rows(state, true);
        aes_sub_bytes(state, kAesInvSbox);
        aes_add_round_key(state, round_keys + 16 * round);
        aes_mix_columns(state, true);
    }
    aes_shift_rows(state, true);
    aes_sub_bytes(state, kAesInvSbox);
    aes_add_round_key(state, round_keys);
}

void codec2_encode_block(const uint8_t* state, const uint8_t* source, uint8_t* destination) {
    const uint32_t src0 = load_u32_le(source + 0);
    const uint32_t src1 = load_u32_le(source + 4);
    const uint32_t src2 = load_u32_le(source + 8);
    const uint32_t src3 = load_u32_le(source + 12);

    uint32_t local_m8 = src0;
    uint32_t local_m4 = src2;
    uint32_t ebx = src3 + state[0x21];
    uint32_t edi = src1 + state[0x20];
    size_t s_index = 0x23;

    for (int round = 0; round < 0x14; ++round) {
        const uint32_t edx = rol32(edi * (edi + edi + 1u), 5);
        const uint32_t eax = rol32(ebx * (ebx + ebx + 1u), 5);

        uint32_t esi = rol32(local_m8 ^ edx, eax & 0x1Fu);
        esi += state[s_index - 1];

        uint32_t new_edi = rol32(local_m4 ^ eax, edx & 0x1Fu);
        new_edi += state[s_index];

        local_m8 = edi;
        local_m4 = ebx;
        ebx = esi;
        edi = new_edi;
        s_index += 2;
    }

    store_u32_le(destination + 0, local_m8 + state[0x4A]);
    store_u32_le(destination + 4, edi);
    store_u32_le(destination + 8, local_m4 + state[0x4B]);
    store_u32_le(destination + 12, ebx);
}

void codec2_decode_block(const uint8_t* state, const uint8_t* source, uint8_t* destination) {
    uint32_t edx = load_u32_le(source + 8) - state[0x4B];
    uint32_t eax = load_u32_le(source + 4);
    uint32_t esi = load_u32_le(source + 12);
    uint32_t ecx = load_u32_le(source + 0) - state[0x4A];
    uint32_t ebx = 0x28;

    uint32_t local_m4 = ebx;
    uint32_t local_m8 = 0;
    uint32_t local_m12 = 0;
    uint32_t local_arg_c = eax;

    while (true) {
        const uint8_t s_byte = state[static_cast<size_t>(ebx) + 0x21];
        uint32_t edi = local_arg_c;

        const uint32_t t = rol32(edx * (edx + edx + 1u), 5);
        local_m8 = edx;
        const uint32_t u = rol32(ecx * (ecx + ecx + 1u), 5);
        local_m12 = ecx;

        edi -= s_byte;
        ebx = local_m4 - 2;
        edi = ror32(edi, u & 0x1Fu);
        edi ^= t;
        local_arg_c = edi;

        esi -= state[static_cast<size_t>(ebx) + 0x22];
        esi = ror32(esi, t & 0x1Fu);
        local_m4 = ebx;
        esi ^= u;
        ecx = esi;

        if (ebx < 2) {
            break;
        }

        eax = local_m12;
        esi = local_m8;
        edx = local_arg_c;
        local_arg_c = eax;
    }

    store_u32_le(destination + 0, ecx);
    store_u32_le(destination + 4, local_m12 - state[0x20]);
    store_u32_le(destination + 8, local_arg_c);
    store_u32_le(destination + 12, local_m8 - state[0x21]);
}

PyObject* module_abi_version(PyObject*, PyObject*) {
    return PyLong_FromLong(kAbiVersion);
}

PyObject* module_build_info(PyObject*, PyObject*) {
    return Py_BuildValue(
        "{s:i,s:s,s:s,s:i}",
        "abi_version",
        kAbiVersion,
        "build_version",
        kBuildVersion,
        "compiler",
#if defined(__clang__)
        "clang",
#elif defined(__GNUC__)
        "gcc",
#else
        "unknown",
#endif
        "pointer_bits",
        static_cast<int>(sizeof(void*) * 8)
    );
}

PyObject* module_character_flow_dispatch(PyObject*, PyObject* arguments) {
    unsigned int opcode = 0;
    Py_buffer body{};
    if (!PyArg_ParseTuple(arguments, "Iy*", &opcode, &body)) {
        return nullptr;
    }

    // The first routing ABI is intentionally a classifier/probe.  It does not
    // mutate server state or build packets; migrated handlers will opt into
    // handled=1 in a later, separately verified operation.
    const int operation_id = character_flow_operation_id(opcode);
    const int handled = operation_id == 1 ? 1 : 0;
    (void)body;
    PyBuffer_Release(&body);
    return Py_BuildValue("(ii)", operation_id, handled);
}

PyObject* module_plan_select_character(PyObject*, PyObject* arguments) {
    Py_buffer body{};
    PyObject* slots_argument = nullptr;
    if (!PyArg_ParseTuple(arguments, "y*O", &body, &slots_argument)) {
        return nullptr;
    }

    PyObject* slots = PySequence_Fast(
        slots_argument,
        "plan_select_character slots must be an iterable of integers"
    );
    if (slots == nullptr) {
        PyBuffer_Release(&body);
        return nullptr;
    }

    const uint16_t requested_slot = body.len >= 2
        ? load_u16_le(reinterpret_cast<const uint8_t*>(body.buf))
        : 0;
    const int parse_source = body.len >= 2 ? 1 : 0;
    const Py_ssize_t count = PySequence_Fast_GET_SIZE(slots);
    PyObject** items = PySequence_Fast_ITEMS(slots);
    Py_ssize_t selected_index = -1;
    int selection_kind = 0;

    for (Py_ssize_t index = 0; index < count; ++index) {
        const long long slot = PyLong_AsLongLong(items[index]);
        if (slot == -1 && PyErr_Occurred()) {
            Py_DECREF(slots);
            PyBuffer_Release(&body);
            return nullptr;
        }
        if (slot == static_cast<long long>(requested_slot)) {
            selected_index = index;
            selection_kind = 1;  // exact DB slot match
            break;
        }
    }

    if (selected_index < 0 && requested_slot < static_cast<uint64_t>(count)) {
        selected_index = static_cast<Py_ssize_t>(requested_slot);
        selection_kind = 2;  // legacy list-index compatibility
    }
    if (selected_index < 0 && count > 0) {
        selected_index = 0;
        selection_kind = 3;  // frozen first-character fallback
    }

    Py_DECREF(slots);
    PyBuffer_Release(&body);
    return Py_BuildValue(
        "(Iiii)",
        static_cast<unsigned int>(requested_slot),
        parse_source,
        static_cast<int>(selected_index),
        selection_kind
    );
}

PyObject* module_concat_buffers(PyObject*, PyObject* argument) {
    PyObject* sequence = PySequence_Fast(
        argument,
        "concat_buffers parts must be an iterable of contiguous buffers"
    );
    if (sequence == nullptr) {
        return nullptr;
    }

    const Py_ssize_t count = PySequence_Fast_GET_SIZE(sequence);
    BufferViews views(count);
    if (!views.acquire(sequence)) {
        Py_DECREF(sequence);
        return nullptr;
    }

    Py_ssize_t total = 0;
    for (size_t index = 0; index < views.size(); ++index) {
        const Py_buffer& view = views[index];
        if (view.len < 0 || view.len > PY_SSIZE_T_MAX - total) {
            Py_DECREF(sequence);
            PyErr_SetString(PyExc_OverflowError, "concat_buffers output is too large");
            return nullptr;
        }
        total += view.len;
    }

    PyObject* output = PyBytes_FromStringAndSize(nullptr, total);
    if (output == nullptr) {
        Py_DECREF(sequence);
        return nullptr;
    }
    auto* destination = reinterpret_cast<uint8_t*>(PyBytes_AS_STRING(output));
    Py_ssize_t offset = 0;
    for (size_t index = 0; index < views.size(); ++index) {
        const Py_buffer& view = views[index];
        if (view.len > 0) {
            std::memcpy(
                destination + offset,
                view.buf,
                static_cast<size_t>(view.len)
            );
            offset += view.len;
        }
    }
    Py_DECREF(sequence);
    return output;
}

PyObject* module_pack_u32le(PyObject*, PyObject* argument) {
    PyObject* sequence = PySequence_Fast(
        argument,
        "pack_u32le values must be an iterable of integers"
    );
    if (sequence == nullptr) {
        return nullptr;
    }
    const Py_ssize_t count = PySequence_Fast_GET_SIZE(sequence);
    if (count > PY_SSIZE_T_MAX / 4) {
        Py_DECREF(sequence);
        PyErr_SetString(PyExc_OverflowError, "pack_u32le output is too large");
        return nullptr;
    }

    PyObject* output = PyBytes_FromStringAndSize(nullptr, count * 4);
    if (output == nullptr) {
        Py_DECREF(sequence);
        return nullptr;
    }
    auto* destination = reinterpret_cast<uint8_t*>(PyBytes_AS_STRING(output));
    PyObject** items = PySequence_Fast_ITEMS(sequence);
    for (Py_ssize_t index = 0; index < count; ++index) {
        const unsigned long long value = PyLong_AsUnsignedLongLong(items[index]);
        if (PyErr_Occurred()) {
            Py_DECREF(output);
            Py_DECREF(sequence);
            return nullptr;
        }
        if (value > std::numeric_limits<uint32_t>::max()) {
            Py_DECREF(output);
            Py_DECREF(sequence);
            PyErr_SetString(PyExc_OverflowError, "pack_u32le value is outside uint32");
            return nullptr;
        }
        store_u32_le(
            destination + static_cast<size_t>(index) * 4,
            static_cast<uint32_t>(value)
        );
    }
    Py_DECREF(sequence);
    return output;
}

PyObject* module_pack_userinfo1_rows(PyObject*, PyObject* argument) {
    PyObject* sequence = PySequence_Fast(
        argument,
        "pack_userinfo1_rows rows must be an iterable of row tuples"
    );
    if (sequence == nullptr) {
        return nullptr;
    }

    const Py_ssize_t count = PySequence_Fast_GET_SIZE(sequence);
    if (count > 0xFF) {
        Py_DECREF(sequence);
        PyErr_SetString(PyExc_ValueError, "USERINFO1 row count must fit uint8");
        return nullptr;
    }

    struct NativeRow {
        uint8_t slot = 0;
        uint32_t item_id = 0;
        uint32_t instance_value = 0;
        uint8_t attr = 0;
        uint16_t durability = 0;
        uint32_t clear_avatar = 0;
        uint32_t enchant = 0;
        uint8_t enchant_upgrade = 0;
        uint8_t amplify_type = 0;
        uint16_t amplify_value = 0;
        uint32_t creature_expire_time = 0;
        uint8_t kind = 0;  // 0 body, 1 avatar, 2 creature
        std::vector<uint8_t> avatar_extra;
    };

    std::vector<NativeRow> rows;
    rows.reserve(static_cast<size_t>(count));
    PyObject** row_objects = PySequence_Fast_ITEMS(sequence);
    Py_ssize_t total = 1;
    constexpr Py_ssize_t kCommonSize = 24;
    constexpr Py_ssize_t kTailSize = 29;
    for (Py_ssize_t index = 0; index < count; ++index) {
        PyObject* row_sequence = PySequence_Fast(
            row_objects[index],
            "USERINFO1 row must be a sequence of 13 fields"
        );
        if (row_sequence == nullptr) {
            Py_DECREF(sequence);
            return nullptr;
        }
        const Py_ssize_t field_count = PySequence_Fast_GET_SIZE(row_sequence);
        if (field_count != 13) {
            Py_DECREF(row_sequence);
            Py_DECREF(sequence);
            PyErr_SetString(PyExc_ValueError, "USERINFO1 row must contain exactly 13 fields");
            return nullptr;
        }
        PyObject** fields = PySequence_Fast_ITEMS(row_sequence);
        NativeRow parsed;
        unsigned long long value = 0;
        if (!read_uint_field(fields[0], 0xFFu, "USERINFO1 slot", &value)) {
            Py_DECREF(row_sequence);
            Py_DECREF(sequence);
            return nullptr;
        }
        parsed.slot = static_cast<uint8_t>(value);
        if (!read_uint_field(fields[1], 0xFFFFFFFFu, "USERINFO1 item_id", &value)) {
            Py_DECREF(row_sequence);
            Py_DECREF(sequence);
            return nullptr;
        }
        parsed.item_id = static_cast<uint32_t>(value);
        if (!read_uint_field(fields[2], 0xFFFFFFFFu, "USERINFO1 instance_value", &value)) {
            Py_DECREF(row_sequence);
            Py_DECREF(sequence);
            return nullptr;
        }
        parsed.instance_value = static_cast<uint32_t>(value);
        if (!read_uint_field(fields[3], 0xFFu, "USERINFO1 attr", &value)) {
            Py_DECREF(row_sequence);
            Py_DECREF(sequence);
            return nullptr;
        }
        parsed.attr = static_cast<uint8_t>(value);
        if (!read_uint_field(fields[4], 0xFFFFu, "USERINFO1 durability", &value)) {
            Py_DECREF(row_sequence);
            Py_DECREF(sequence);
            return nullptr;
        }
        parsed.durability = static_cast<uint16_t>(value);
        if (!read_uint_field(fields[5], 0xFFFFFFFFu, "USERINFO1 clear_avatar", &value)) {
            Py_DECREF(row_sequence);
            Py_DECREF(sequence);
            return nullptr;
        }
        parsed.clear_avatar = static_cast<uint32_t>(value);
        if (!read_uint_field(fields[6], 0xFFFFFFFFu, "USERINFO1 enchant", &value)) {
            Py_DECREF(row_sequence);
            Py_DECREF(sequence);
            return nullptr;
        }
        parsed.enchant = static_cast<uint32_t>(value);
        if (!read_uint_field(fields[7], 0xFFu, "USERINFO1 enchant_upgrade", &value)) {
            Py_DECREF(row_sequence);
            Py_DECREF(sequence);
            return nullptr;
        }
        parsed.enchant_upgrade = static_cast<uint8_t>(value);
        if (!read_uint_field(fields[8], 0xFFu, "USERINFO1 amplify_type", &value)) {
            Py_DECREF(row_sequence);
            Py_DECREF(sequence);
            return nullptr;
        }
        parsed.amplify_type = static_cast<uint8_t>(value);
        if (!read_uint_field(fields[9], 0xFFFFu, "USERINFO1 amplify_value", &value)) {
            Py_DECREF(row_sequence);
            Py_DECREF(sequence);
            return nullptr;
        }
        parsed.amplify_value = static_cast<uint16_t>(value);
        if (!read_uint_field(fields[10], 0xFFFFFFFFu, "USERINFO1 creature_expire_time", &value)) {
            Py_DECREF(row_sequence);
            Py_DECREF(sequence);
            return nullptr;
        }
        parsed.creature_expire_time = static_cast<uint32_t>(value);
        if (!read_uint_field(fields[11], 2u, "USERINFO1 row kind", &value)) {
            Py_DECREF(row_sequence);
            Py_DECREF(sequence);
            return nullptr;
        }
        parsed.kind = static_cast<uint8_t>(value);

        if (parsed.kind == 1) {
            Py_buffer extra{};
            if (PyObject_GetBuffer(fields[12], &extra, PyBUF_CONTIG_RO) != 0) {
                Py_DECREF(row_sequence);
                Py_DECREF(sequence);
                return nullptr;
            }
            if (extra.len < 0 || extra.len > PY_SSIZE_T_MAX - total) {
                PyBuffer_Release(&extra);
                Py_DECREF(row_sequence);
                Py_DECREF(sequence);
                PyErr_SetString(PyExc_OverflowError, "USERINFO1 avatar row output is too large");
                return nullptr;
            }
            if (extra.len > 0) {
                const auto* source = static_cast<const uint8_t*>(extra.buf);
                parsed.avatar_extra.assign(source, source + extra.len);
            }
            total += extra.len;
            PyBuffer_Release(&extra);
        }
        const Py_ssize_t row_size = kCommonSize + kTailSize + (parsed.kind == 1 ? 0 : parsed.kind == 2 ? 4 : 0);
        if (row_size > PY_SSIZE_T_MAX - total) {
            Py_DECREF(row_sequence);
            Py_DECREF(sequence);
            PyErr_SetString(PyExc_OverflowError, "USERINFO1 output is too large");
            return nullptr;
        }
        total += row_size;
        rows.push_back(std::move(parsed));
        Py_DECREF(row_sequence);
    }

    PyObject* output = PyBytes_FromStringAndSize(nullptr, total);
    if (output == nullptr) {
        Py_DECREF(sequence);
        return nullptr;
    }
    auto* destination = reinterpret_cast<uint8_t*>(PyBytes_AS_STRING(output));
    Py_ssize_t offset = 0;
    destination[offset++] = static_cast<uint8_t>(count);
    for (const NativeRow& row : rows) {
        destination[offset++] = row.slot;
        store_u32_le(destination + offset, row.item_id);
        offset += 4;
        store_u32_le(destination + offset, row.instance_value);
        offset += 4;
        destination[offset++] = row.attr;
        store_u16_le(destination + offset, row.durability);
        offset += 2;
        store_u32_le(destination + offset, row.clear_avatar);
        offset += 4;
        store_u32_le(destination + offset, row.enchant);
        offset += 4;
        destination[offset++] = row.enchant_upgrade;
        destination[offset++] = row.amplify_type;
        store_u16_le(destination + offset, row.amplify_value);
        offset += 2;
        if (row.kind == 1 && !row.avatar_extra.empty()) {
            std::memcpy(
                destination + offset,
                row.avatar_extra.data(),
                row.avatar_extra.size()
            );
            offset += static_cast<Py_ssize_t>(row.avatar_extra.size());
        } else if (row.kind == 2) {
            store_u32_le(destination + offset, row.creature_expire_time);
            offset += 4;
        }
        std::memset(destination + offset, 0, static_cast<size_t>(kTailSize));
        offset += kTailSize;
    }
    Py_DECREF(sequence);
    return output;
}

PyObject* module_unpack_u32le(PyObject*, PyObject* argument) {
    Py_buffer view{};
    if (PyObject_GetBuffer(argument, &view, PyBUF_CONTIG_RO) != 0) {
        return nullptr;
    }
    if (view.len < 0 || (view.len & 3) != 0) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "unpack_u32le input length must be divisible by 4");
        return nullptr;
    }
    const Py_ssize_t count = view.len / 4;
    PyObject* output = PyTuple_New(count);
    if (output == nullptr) {
        PyBuffer_Release(&view);
        return nullptr;
    }
    const auto* source = static_cast<const uint8_t*>(view.buf);
    for (Py_ssize_t index = 0; index < count; ++index) {
        PyObject* value = PyLong_FromUnsignedLong(
            load_u32_le(source + static_cast<size_t>(index) * 4)
        );
        if (value == nullptr) {
            Py_DECREF(output);
            PyBuffer_Release(&view);
            return nullptr;
        }
        PyTuple_SET_ITEM(output, index, value);
    }
    PyBuffer_Release(&view);
    return output;
}

PyObject* module_build_game_header(PyObject*, PyObject* arguments) {
    unsigned int flag = 0;
    unsigned int opcode = 0;
    unsigned int total = 0;
    unsigned int checksum = 0;
    unsigned int sub_or_seq = 0;
    PyObject* reserved_object = nullptr;
    int compact = 0;
    if (!PyArg_ParseTuple(
            arguments,
            "IIIIIOp",
            &flag,
            &opcode,
            &total,
            &checksum,
            &sub_or_seq,
            &reserved_object,
            &compact
        )) {
        return nullptr;
    }
    if (flag > 0xFFu || opcode > 0xFFFFu || sub_or_seq > 0xFFFFu) {
        PyErr_SetString(PyExc_OverflowError, "game header field is outside its wire width");
        return nullptr;
    }

    Py_buffer reserved{};
    if (PyObject_GetBuffer(reserved_object, &reserved, PyBUF_CONTIG_RO) != 0) {
        return nullptr;
    }
    if (!compact && reserved.len != 3) {
        PyBuffer_Release(&reserved);
        PyErr_SetString(PyExc_ValueError, "reserved must be exactly 3 bytes");
        return nullptr;
    }
    const Py_ssize_t header_size = compact ? 13 : 16;
    PyObject* output = PyBytes_FromStringAndSize(nullptr, header_size);
    if (output == nullptr) {
        PyBuffer_Release(&reserved);
        return nullptr;
    }
    auto* destination = reinterpret_cast<uint8_t*>(PyBytes_AS_STRING(output));
    destination[0] = static_cast<uint8_t>(flag);
    store_u16_le(destination + 1, static_cast<uint16_t>(opcode));
    store_u32_le(destination + 3, static_cast<uint32_t>(total));
    store_u32_le(destination + 7, static_cast<uint32_t>(checksum));
    store_u16_le(destination + 11, static_cast<uint16_t>(sub_or_seq));
    if (!compact) {
        std::memcpy(destination + 13, reserved.buf, 3);
    }
    PyBuffer_Release(&reserved);
    return output;
}

PyObject* module_parse_game_header(PyObject*, PyObject* argument) {
    Py_buffer view{};
    if (PyObject_GetBuffer(argument, &view, PyBUF_CONTIG_RO) != 0) {
        return nullptr;
    }
    if (view.len < 13) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "game header input must contain at least 13 bytes");
        return nullptr;
    }

    const auto* source = static_cast<const uint8_t*>(view.buf);
    const uint32_t total = load_u32_le(source + 3);
    const int header_size = (total >= 16 && view.len >= 16) ? 16 : 13;
    PyObject* output = Py_BuildValue(
        "(IIIIII)",
        static_cast<unsigned int>(source[0]),
        static_cast<unsigned int>(load_u16_le(source + 1)),
        static_cast<unsigned int>(total),
        static_cast<unsigned int>(load_u32_le(source + 7)),
        static_cast<unsigned int>(load_u16_le(source + 11)),
        static_cast<unsigned int>(header_size)
    );
    PyBuffer_Release(&view);
    return output;
}

PyObject* module_aes128_ecb_crypt(PyObject*, PyObject* arguments, bool decrypt) {
    PyObject* data_object = nullptr;
    PyObject* key_object = nullptr;
    if (!PyArg_ParseTuple(arguments, "OO", &data_object, &key_object)) {
        return nullptr;
    }

    Py_buffer data{};
    Py_buffer key{};
    if (PyObject_GetBuffer(data_object, &data, PyBUF_CONTIG_RO) != 0) {
        return nullptr;
    }
    if (PyObject_GetBuffer(key_object, &key, PyBUF_CONTIG_RO) != 0) {
        PyBuffer_Release(&data);
        return nullptr;
    }
    if (key.len != 16) {
        PyBuffer_Release(&key);
        PyBuffer_Release(&data);
        PyErr_SetString(PyExc_ValueError, "AES-128 key must be exactly 16 bytes");
        return nullptr;
    }
    if (data.len < 0 || (data.len & 15) != 0) {
        PyBuffer_Release(&key);
        PyBuffer_Release(&data);
        PyErr_SetString(PyExc_ValueError, "AES-128 ECB input must be block aligned");
        return nullptr;
    }

    PyObject* output = PyBytes_FromStringAndSize(nullptr, data.len);
    if (output == nullptr) {
        PyBuffer_Release(&key);
        PyBuffer_Release(&data);
        return nullptr;
    }
    uint8_t round_keys[176];
    aes128_expand_key(static_cast<const uint8_t*>(key.buf), round_keys);
    const auto* source = static_cast<const uint8_t*>(data.buf);
    auto* destination = reinterpret_cast<uint8_t*>(PyBytes_AS_STRING(output));
    for (Py_ssize_t offset = 0; offset < data.len; offset += 16) {
        std::memcpy(destination + offset, source + offset, 16);
        if (decrypt) {
            aes128_decrypt_block(destination + offset, round_keys);
        } else {
            aes128_encrypt_block(destination + offset, round_keys);
        }
    }
    PyBuffer_Release(&key);
    PyBuffer_Release(&data);
    return output;
}

PyObject* module_aes128_ecb_encrypt(PyObject* self, PyObject* arguments) {
    return module_aes128_ecb_crypt(self, arguments, false);
}

PyObject* module_aes128_ecb_decrypt(PyObject* self, PyObject* arguments) {
    return module_aes128_ecb_crypt(self, arguments, true);
}

PyObject* module_codec2_crypt(PyObject*, PyObject* arguments, bool decrypt) {
    PyObject* state_object = nullptr;
    PyObject* data_object = nullptr;
    if (!PyArg_ParseTuple(arguments, "OO", &state_object, &data_object)) {
        return nullptr;
    }

    Py_buffer state{};
    Py_buffer data{};
    if (PyObject_GetBuffer(state_object, &state, PyBUF_CONTIG_RO) != 0) {
        return nullptr;
    }
    if (PyObject_GetBuffer(data_object, &data, PyBUF_CONTIG_RO) != 0) {
        PyBuffer_Release(&state);
        return nullptr;
    }
    if (data.len < 0 || (data.len & 15) != 0) {
        PyBuffer_Release(&data);
        PyBuffer_Release(&state);
        PyErr_SetString(PyExc_ValueError, "codec2 input must be 0x10-byte aligned");
        return nullptr;
    }
    // The Python reference returns an empty result without touching state when
    // the aligned input has no blocks. Preserve that edge behavior exactly.
    if (data.len != 0 && state.len < 0x4C) {
        PyBuffer_Release(&data);
        PyBuffer_Release(&state);
        PyErr_SetString(PyExc_ValueError, "codec2 state must be at least 0x4C bytes");
        return nullptr;
    }

    PyObject* output = PyBytes_FromStringAndSize(nullptr, data.len);
    if (output == nullptr) {
        PyBuffer_Release(&data);
        PyBuffer_Release(&state);
        return nullptr;
    }
    const auto* state_bytes = static_cast<const uint8_t*>(state.buf);
    const auto* source = static_cast<const uint8_t*>(data.buf);
    auto* destination = reinterpret_cast<uint8_t*>(PyBytes_AS_STRING(output));
    for (Py_ssize_t offset = 0; offset < data.len; offset += 16) {
        if (decrypt) {
            codec2_decode_block(state_bytes, source + offset, destination + offset);
        } else {
            codec2_encode_block(state_bytes, source + offset, destination + offset);
        }
    }
    PyBuffer_Release(&data);
    PyBuffer_Release(&state);
    return output;
}

PyObject* module_codec2_encode(PyObject* self, PyObject* arguments) {
    return module_codec2_crypt(self, arguments, false);
}

PyObject* module_codec2_decode(PyObject* self, PyObject* arguments) {
    return module_codec2_crypt(self, arguments, true);
}

PyObject* module_codec2_build_state(PyObject*, PyObject* argument) {
    Py_buffer key{};
    if (PyObject_GetBuffer(argument, &key, PyBUF_CONTIG_RO) != 0) {
        return nullptr;
    }
    if (key.len < 0x20) {
        PyBuffer_Release(&key);
        PyErr_SetString(PyExc_ValueError, "codec2 key material must contain at least 0x20 bytes");
        return nullptr;
    }

    constexpr Py_ssize_t kStateSize = 0x4C;
    PyObject* output = PyBytes_FromStringAndSize(nullptr, kStateSize);
    if (output == nullptr) {
        PyBuffer_Release(&key);
        return nullptr;
    }
    auto* state = reinterpret_cast<uint8_t*>(PyBytes_AS_STRING(output));
    std::memset(state, 0, static_cast<size_t>(kStateSize));
    const Py_ssize_t copied = std::min(key.len, kStateSize);
    std::memcpy(state, key.buf, static_cast<size_t>(copied));

    uint32_t key_words[8] = {};
    for (int position = 0x1F; position >= 0; --position) {
        const size_t index = static_cast<size_t>(position >> 2);
        key_words[index] = static_cast<uint32_t>(
            state[position] + (key_words[index] << 8)
        );
    }

    state[0x20] = 0x63;
    for (size_t index = 1; index < 0x2C; ++index) {
        state[0x20 + index] = static_cast<uint8_t>(state[0x1F + index] - 0x47u);
    }

    uint32_t a = 0;
    uint32_t b = 0;
    size_t key_index = 0;
    size_t state_index = 0;
    for (size_t iteration = 0; iteration < 0x2C * 3; ++iteration) {
        const uint32_t mixed_state = rol32(
            static_cast<uint32_t>(state[0x20 + state_index]) + a + b,
            3
        );
        state[0x20 + state_index] = static_cast<uint8_t>(mixed_state);
        a = state[0x20 + state_index];

        const uint32_t mixed_key = rol32(
            key_words[key_index] + a + b,
            (a + b) & 0x1Fu
        );
        key_words[key_index] = mixed_key;
        b = mixed_key;

        state_index = (state_index + 1) % 0x2C;
        key_index = (key_index + 1) % 8;
    }

    PyBuffer_Release(&key);
    return output;
}

PyObject* module_codec13_build_state(PyObject*, PyObject* argument) {
    Py_buffer key{};
    if (PyObject_GetBuffer(argument, &key, PyBUF_CONTIG_RO) != 0) {
        return nullptr;
    }
    if (key.len != 0x28) {
        PyBuffer_Release(&key);
        PyErr_SetString(PyExc_ValueError, "codec13 key material must be exactly 0x28 bytes");
        return nullptr;
    }

    uint32_t rounds = 0;
    uint32_t schedule[8] = {};
    codec13_schedule_from_key(static_cast<const uint8_t*>(key.buf), &rounds, schedule);
    PyObject* output = PyBytes_FromStringAndSize(nullptr, 0x24);
    if (output == nullptr) {
        PyBuffer_Release(&key);
        return nullptr;
    }
    auto* destination = reinterpret_cast<uint8_t*>(PyBytes_AS_STRING(output));
    store_u32_le(destination, rounds);
    for (int index = 0; index < 8; ++index) {
        store_u32_le(destination + 4 + index * 4, schedule[index]);
    }
    PyBuffer_Release(&key);
    return output;
}

PyObject* module_codec13_crypt(PyObject*, PyObject* arguments, bool decrypt) {
    PyObject* state_object = nullptr;
    PyObject* data_object = nullptr;
    if (!PyArg_ParseTuple(arguments, "OO", &state_object, &data_object)) {
        return nullptr;
    }
    Py_buffer state{};
    Py_buffer data{};
    if (PyObject_GetBuffer(state_object, &state, PyBUF_CONTIG_RO) != 0) {
        return nullptr;
    }
    if (PyObject_GetBuffer(data_object, &data, PyBUF_CONTIG_RO) != 0) {
        PyBuffer_Release(&state);
        return nullptr;
    }
    if (state.len != 0x24 || data.len < 0 || (data.len & 7) != 0) {
        PyBuffer_Release(&data);
        PyBuffer_Release(&state);
        PyErr_SetString(PyExc_ValueError, "codec13 state/data has an invalid size");
        return nullptr;
    }
    PyObject* output = PyBytes_FromStringAndSize(nullptr, data.len);
    if (output == nullptr) {
        PyBuffer_Release(&data);
        PyBuffer_Release(&state);
        return nullptr;
    }
    const auto* state_bytes = static_cast<const uint8_t*>(state.buf);
    uint32_t rounds = load_u32_le(state_bytes);
    uint32_t schedule[8] = {};
    for (int index = 0; index < 8; ++index) {
        schedule[index] = load_u32_le(state_bytes + 4 + index * 4);
    }
    const auto* source = static_cast<const uint8_t*>(data.buf);
    auto* destination = reinterpret_cast<uint8_t*>(PyBytes_AS_STRING(output));
    for (Py_ssize_t offset = 0; offset < data.len; offset += 8) {
        if (decrypt) {
            codec13_decode_block(rounds, schedule, source + offset, destination + offset);
        } else {
            codec13_encode_block(rounds, schedule, source + offset, destination + offset);
        }
    }
    PyBuffer_Release(&data);
    PyBuffer_Release(&state);
    return output;
}

PyObject* module_codec13_encode(PyObject* self, PyObject* arguments) {
    return module_codec13_crypt(self, arguments, false);
}

PyObject* module_codec13_decode(PyObject* self, PyObject* arguments) {
    return module_codec13_crypt(self, arguments, true);
}

PyMethodDef kModuleMethods[] = {
    {"abi_version", module_abi_version, METH_NOARGS, "Return the private native-core ABI version."},
    {"build_info", module_build_info, METH_NOARGS, "Return stable native-core build metadata."},
    {"character_flow_dispatch", module_character_flow_dispatch, METH_VARARGS, "Classify a character-flow request and return (operation_id, handled)."},
    {"plan_select_character", module_plan_select_character, METH_VARARGS, "Plan SELECT_CHARACTER request parsing and roster resolution."},
    {"concat_buffers", module_concat_buffers, METH_O, "Join contiguous buffers into one bytes object."},
    {"pack_u32le", module_pack_u32le, METH_O, "Pack an integer iterable as little-endian uint32 values."},
    {"pack_userinfo1_rows", module_pack_userinfo1_rows, METH_O, "Pack normalized Stage3 USERINFO1 equipment rows."},
    {"unpack_u32le", module_unpack_u32le, METH_O, "Unpack a buffer of little-endian uint32 values."},
    {"build_game_header", module_build_game_header, METH_VARARGS, "Build a compact or full Game wire header."},
    {"parse_game_header", module_parse_game_header, METH_O, "Parse the fixed Game wire header fields."},
    {"aes128_ecb_encrypt", module_aes128_ecb_encrypt, METH_VARARGS, "Encrypt block-aligned data with AES-128 ECB."},
    {"aes128_ecb_decrypt", module_aes128_ecb_decrypt, METH_VARARGS, "Decrypt block-aligned data with AES-128 ECB."},
    {"codec2_build_state", module_codec2_build_state, METH_O, "Build the frozen codec2 state from key material."},
    {"codec2_encode", module_codec2_encode, METH_VARARGS, "Encode block-aligned data with the frozen codec2 transform."},
    {"codec2_decode", module_codec2_decode, METH_VARARGS, "Decode block-aligned data with the frozen codec2 transform."},
    {"codec13_build_state", module_codec13_build_state, METH_O, "Build the frozen codec13 state from key material."},
    {"codec13_encode", module_codec13_encode, METH_VARARGS, "Encode block-aligned data with the frozen codec13 transform."},
    {"codec13_decode", module_codec13_decode, METH_VARARGS, "Decode block-aligned data with the frozen codec13 transform."},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef kModuleDefinition = {
    PyModuleDef_HEAD_INIT,
    "usdof_core",
    "USDOF server-side native compute core.",
    -1,
    kModuleMethods,
    nullptr,
    nullptr,
    nullptr,
    nullptr,
};

}  // namespace

PyMODINIT_FUNC PyInit_usdof_core() {
    return PyModule_Create(&kModuleDefinition);
}
