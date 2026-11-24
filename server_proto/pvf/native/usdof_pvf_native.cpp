#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <windows.h>

#include <algorithm>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <limits>
#include <memory>
#include <new>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {

constexpr int kAbiVersion = 1;
constexpr uint32_t kLcgMultiplier = 214013u;
constexpr char kDirectoryMagic[8] = {'U', 'S', 'D', 'P', 'V', 'F', 'D', '1'};
constexpr uint32_t kDirectorySchema = 1;
constexpr size_t kEntrySize = 24;
constexpr uint64_t kMaxDirectoryBlob = 1024ull * 1024ull * 1024ull;

struct BufferView {
    Py_buffer view{};
    bool active = false;

    ~BufferView() {
        if (active) {
            PyBuffer_Release(&view);
        }
    }

    bool acquire(PyObject* object) {
        if (PyObject_GetBuffer(object, &view, PyBUF_CONTIG_RO) != 0) {
            return false;
        }
        active = true;
        return true;
    }

    const uint8_t* data() const {
        return static_cast<const uint8_t*>(view.buf);
    }

    size_t size() const {
        return static_cast<size_t>(view.len);
    }
};

struct Entry {
    int32_t name_offset = 0;
    int32_t path_offset = 0;
    int32_t chunk_index = 0;
    int32_t data_offset = 0;
    int32_t data_size = 0;
    int32_t data_type = 0;
};

struct DirectoryData {
    std::vector<Entry> entries;
    std::vector<std::string> paths;
    std::vector<std::string> normalized_paths;
    std::unordered_map<std::string, size_t> lookup;
};

typedef struct {
    PyObject_HEAD
    DirectoryData* data;
} NativeDirectoryObject;

PyTypeObject NativeDirectoryType = {
    PyVarObject_HEAD_INIT(nullptr, 0)
};

uint32_t load_u32(const uint8_t* source) {
    return static_cast<uint32_t>(source[0]) |
           (static_cast<uint32_t>(source[1]) << 8) |
           (static_cast<uint32_t>(source[2]) << 16) |
           (static_cast<uint32_t>(source[3]) << 24);
}

uint64_t load_u64(const uint8_t* source) {
    uint64_t value = 0;
    for (int index = 7; index >= 0; --index) {
        value = (value << 8) | source[index];
    }
    return value;
}

void append_u32(std::vector<uint8_t>& output, uint32_t value) {
    output.push_back(static_cast<uint8_t>(value));
    output.push_back(static_cast<uint8_t>(value >> 8));
    output.push_back(static_cast<uint8_t>(value >> 16));
    output.push_back(static_cast<uint8_t>(value >> 24));
}

void append_u64(std::vector<uint8_t>& output, uint64_t value) {
    for (int index = 0; index < 8; ++index) {
        output.push_back(static_cast<uint8_t>(value >> (index * 8)));
    }
}

std::string normalize_path(std::string value) {
    for (char& character : value) {
        if (character == '\\') {
            character = '/';
        } else if (character >= 'A' && character <= 'Z') {
            character = static_cast<char>(character + ('a' - 'A'));
        }
    }
    size_t begin = 0;
    while (begin < value.size() && value[begin] == '/') {
        ++begin;
    }
    size_t end = value.size();
    while (end > begin && value[end - 1] == '/') {
        --end;
    }
    return value.substr(begin, end - begin);
}

bool python_path_to_utf8(PyObject* object, std::string& output) {
    if (!PyUnicode_Check(object)) {
        PyErr_SetString(PyExc_TypeError, "path must be str");
        return false;
    }
    Py_ssize_t size = 0;
    const char* data = PyUnicode_AsUTF8AndSize(object, &size);
    if (data == nullptr) {
        return false;
    }
    output.assign(data, static_cast<size_t>(size));
    return true;
}

bool contains_cjk(const std::wstring& value) {
    for (wchar_t character : value) {
        const uint32_t code = static_cast<uint32_t>(character);
        if ((code >= 0x3400u && code <= 0x4DBFu) ||
            (code >= 0x4E00u && code <= 0x9FFFu) ||
            (code >= 0xF900u && code <= 0xFAFFu)) {
            return true;
        }
    }
    return false;
}

int east_asian_decode_score(const std::wstring& value) {
    int score = 0;
    for (wchar_t character : value) {
        const uint32_t code = static_cast<uint32_t>(character);
        if ((code >= 0x3400u && code <= 0x4DBFu) ||
            (code >= 0x4E00u && code <= 0x9FFFu)) {
            score += 4;
        } else if (code >= 0xF900u && code <= 0xFAFFu) {
            score += 1;
        }
    }
    return score;
}

bool contains_pvf_han_phrase(const std::wstring& value) {
    // Tie-break only: these phrases identify translated stackable/skill text
    // when the same byte sequence is valid in both CP949 and GBK.
    static constexpr const wchar_t* kPhrases[] = {
        L"\x7ECF\x9A8C", L"\x80F6\x56CA", L"\x5347\x7EA7", L"\x5F3A\x5316",
        L"\x589E\x5E45", L"\x6280\x80FD", L"\x70B9\x6570", L"\x7269\x54C1",
        L"\x9053\x5177", L"\x88C5\x5907", L"\x7B49\x7EA7", L"\x89D2\x8272",
        L"\x4EFB\x52A1", L"\x5956\x52B1", L"\x4F7F\x7528", L"\x589E\x52A0",
        L"\x6D88\x8017", L"\x6982\x7387", L"\x6210\x529F", L"\x5931\x8D25",
        L"\x91D1\x5E01", L"\x653B\x51FB", L"\x9632\x5FA1", L"\x590D\x6D3B",
        L"\x5238", L"\x4E66", L"\x70B9",
    };
    for (const wchar_t* phrase : kPhrases) {
        if (value.find(phrase) != std::wstring::npos) {
            return true;
        }
    }
    return false;
}

bool multibyte_to_wide(
    UINT code_page,
    DWORD flags,
    const char* data,
    int size,
    std::wstring& output
) {
    if (size == 0) {
        output.clear();
        return true;
    }
    const int required = MultiByteToWideChar(code_page, flags, data, size, nullptr, 0);
    if (required <= 0) {
        return false;
    }
    output.resize(static_cast<size_t>(required));
    return MultiByteToWideChar(
               code_page,
               flags,
               data,
               size,
               &output[0],
               required
           ) == required;
}

std::string wide_to_utf8(const std::wstring& value) {
    if (value.empty()) {
        return {};
    }
    const int source_size = static_cast<int>(value.size());
    const int required = WideCharToMultiByte(
        CP_UTF8,
        WC_ERR_INVALID_CHARS,
        value.data(),
        source_size,
        nullptr,
        0,
        nullptr,
        nullptr
    );
    if (required <= 0) {
        return {};
    }
    std::string output(static_cast<size_t>(required), '\0');
    if (WideCharToMultiByte(
            CP_UTF8,
            WC_ERR_INVALID_CHARS,
            value.data(),
            source_size,
            &output[0],
            required,
            nullptr,
            nullptr
        ) != required) {
        return {};
    }
    return output;
}

std::string decode_narrow_to_utf8(const uint8_t* data, size_t size) {
    if (size > static_cast<size_t>(std::numeric_limits<int>::max())) {
        return {};
    }
    const char* source = reinterpret_cast<const char*>(data);
    const int source_size = static_cast<int>(size);
    std::wstring decoded;
    if (multibyte_to_wide(CP_UTF8, MB_ERR_INVALID_CHARS, source, source_size, decoded)) {
        return std::string(source, size);
    }
    std::string best_candidate;
    int best_score = -1;
    for (UINT code_page : {949u, 936u}) {
        if (multibyte_to_wide(code_page, MB_ERR_INVALID_CHARS, source, source_size, decoded) &&
            contains_cjk(decoded)) {
            const int score = east_asian_decode_score(decoded) +
                (contains_pvf_han_phrase(decoded) ? 32 : 0);
            std::string candidate = wide_to_utf8(decoded);
            if (!candidate.empty() && score > best_score) {
                best_candidate = std::move(candidate);
                best_score = score;
            }
        }
    }
    if (best_score >= 0) {
        return best_candidate;
    }
    if (multibyte_to_wide(437u, 0, source, source_size, decoded)) {
        return wide_to_utf8(decoded);
    }
    return {};
}

std::string decode_utf16_to_utf8(const uint8_t* data, size_t size) {
    if ((size & 1u) != 0 || size / 2 > static_cast<size_t>(std::numeric_limits<int>::max())) {
        return {};
    }
    std::wstring decoded;
    decoded.reserve(size / 2);
    for (size_t offset = 0; offset < size; offset += 2) {
        decoded.push_back(static_cast<wchar_t>(
            static_cast<uint16_t>(data[offset]) |
            (static_cast<uint16_t>(data[offset + 1]) << 8)
        ));
    }
    return wide_to_utf8(decoded);
}

std::string bad_name_offset(int32_t magic_offset) {
    char buffer[80]{};
    if (magic_offset < 0) {
        std::snprintf(
            buffer,
            sizeof(buffer),
            "<negative-name-offset:%d>",
            static_cast<int>(magic_offset)
        );
    } else {
        std::snprintf(
            buffer,
            sizeof(buffer),
            "<bad-name-offset:0x%x>",
            static_cast<unsigned int>(magic_offset)
        );
    }
    return buffer;
}

std::string resolve_name(
    int32_t magic_offset,
    const uint8_t* utf8_pool,
    size_t utf8_size,
    const uint8_t* utf16_pool,
    size_t utf16_size
) {
    if (magic_offset < 0) {
        return bad_name_offset(magic_offset);
    }
    if ((magic_offset & 1) == 0) {
        const size_t offset = static_cast<uint32_t>(magic_offset) >> 1;
        if (offset >= utf8_size) {
            return bad_name_offset(magic_offset);
        }
        size_t end = offset;
        while (end < utf8_size && utf8_pool[end] != 0) {
            ++end;
        }
        return decode_narrow_to_utf8(utf8_pool + offset, end - offset);
    }
    const uint64_t offset64 =
        static_cast<uint64_t>(static_cast<uint32_t>(magic_offset) >> 1) * 2u;
    if (offset64 >= utf16_size) {
        return bad_name_offset(magic_offset);
    }
    const size_t offset = static_cast<size_t>(offset64);
    size_t end = offset;
    while (end + 1 < utf16_size) {
        if (utf16_pool[end] == 0 && utf16_pool[end + 1] == 0) {
            break;
        }
        end += 2;
    }
    return decode_utf16_to_utf8(utf16_pool + offset, end - offset);
}

bool build_lookup(DirectoryData& directory) {
    if (directory.entries.size() != directory.paths.size()) {
        PyErr_SetString(PyExc_ValueError, "directory entry/path count mismatch");
        return false;
    }
    directory.lookup.clear();
    directory.normalized_paths.clear();
    directory.normalized_paths.reserve(directory.paths.size());
    directory.lookup.reserve(directory.paths.size() * 2 + 1);
    for (size_t index = 0; index < directory.paths.size(); ++index) {
        directory.normalized_paths.push_back(normalize_path(directory.paths[index]));
        directory.lookup[directory.normalized_paths.back()] = index;
    }
    return true;
}

NativeDirectoryObject* new_directory(std::unique_ptr<DirectoryData> data) {
    auto* object = PyObject_New(NativeDirectoryObject, &NativeDirectoryType);
    if (object == nullptr) {
        return nullptr;
    }
    object->data = data.release();
    return object;
}

void directory_dealloc(NativeDirectoryObject* self) {
    delete self->data;
    self->data = nullptr;
    PyObject_Del(self);
}

Py_ssize_t directory_length(PyObject* object) {
    auto* self = reinterpret_cast<NativeDirectoryObject*>(object);
    if (self->data->entries.size() > static_cast<size_t>(PY_SSIZE_T_MAX)) {
        PyErr_SetString(PyExc_OverflowError, "directory is too large");
        return -1;
    }
    return static_cast<Py_ssize_t>(self->data->entries.size());
}

bool parse_index(PyObject* object, size_t size, size_t& index) {
    const Py_ssize_t value = PyNumber_AsSsize_t(object, PyExc_IndexError);
    if (value == -1 && PyErr_Occurred()) {
        return false;
    }
    if (value < 0 || static_cast<size_t>(value) >= size) {
        PyErr_SetString(PyExc_IndexError, "directory index out of range");
        return false;
    }
    index = static_cast<size_t>(value);
    return true;
}

PyObject* directory_lookup(NativeDirectoryObject* self, PyObject* argument) {
    std::string path;
    if (!python_path_to_utf8(argument, path)) {
        return nullptr;
    }
    const auto found = self->data->lookup.find(normalize_path(std::move(path)));
    if (found == self->data->lookup.end()) {
        Py_RETURN_NONE;
    }
    return PyLong_FromSize_t(found->second);
}

PyObject* directory_path_at(NativeDirectoryObject* self, PyObject* argument) {
    size_t index = 0;
    if (!parse_index(argument, self->data->paths.size(), index)) {
        return nullptr;
    }
    const std::string& path = self->data->paths[index];
    return PyUnicode_DecodeUTF8(path.data(), static_cast<Py_ssize_t>(path.size()), "strict");
}

PyObject* entry_tuple(const Entry& entry) {
    return Py_BuildValue(
        "(iiiiii)",
        entry.name_offset,
        entry.path_offset,
        entry.chunk_index,
        entry.data_offset,
        entry.data_size,
        entry.data_type
    );
}

PyObject* directory_entry_at(NativeDirectoryObject* self, PyObject* argument) {
    size_t index = 0;
    if (!parse_index(argument, self->data->entries.size(), index)) {
        return nullptr;
    }
    return entry_tuple(self->data->entries[index]);
}

PyObject* make_path_iterator(const std::vector<std::string>& paths) {
    PyObject* list = PyList_New(static_cast<Py_ssize_t>(paths.size()));
    if (list == nullptr) {
        return nullptr;
    }
    for (size_t index = 0; index < paths.size(); ++index) {
        const std::string& path = paths[index];
        PyObject* value = PyUnicode_DecodeUTF8(
            path.data(),
            static_cast<Py_ssize_t>(path.size()),
            "strict"
        );
        if (value == nullptr) {
            Py_DECREF(list);
            return nullptr;
        }
        PyList_SET_ITEM(list, static_cast<Py_ssize_t>(index), value);
    }
    PyObject* iterator = PyObject_GetIter(list);
    Py_DECREF(list);
    return iterator;
}

PyObject* directory_iter_prefix(NativeDirectoryObject* self, PyObject* argument) {
    std::string prefix;
    if (!python_path_to_utf8(argument, prefix)) {
        return nullptr;
    }
    prefix = normalize_path(std::move(prefix));
    std::vector<std::string> matches;
    for (size_t index = 0; index < self->data->paths.size(); ++index) {
        const std::string& normalized = self->data->normalized_paths[index];
        if (normalized.compare(0, prefix.size(), prefix) == 0) {
            matches.push_back(self->data->paths[index]);
        }
    }
    return make_path_iterator(matches);
}

PyObject* directory_search(NativeDirectoryObject* self, PyObject* args, PyObject* kwargs) {
    PyObject* query_object = nullptr;
    PyObject* prefix_object = Py_None;
    Py_ssize_t limit = 0;
    static const char* keywords[] = {"query", "prefix", "limit", nullptr};
    if (!PyArg_ParseTupleAndKeywords(
            args,
            kwargs,
            "O|On:search",
            const_cast<char**>(keywords),
            &query_object,
            &prefix_object,
            &limit
        )) {
        return nullptr;
    }
    if (limit < 0) {
        PyErr_SetString(PyExc_ValueError, "limit must not be negative");
        return nullptr;
    }
    std::string query;
    if (!python_path_to_utf8(query_object, query)) {
        return nullptr;
    }
    query = normalize_path(std::move(query));
    std::string prefix;
    if (prefix_object != Py_None) {
        if (!python_path_to_utf8(prefix_object, prefix)) {
            return nullptr;
        }
        prefix = normalize_path(std::move(prefix));
    }
    std::vector<std::string> matches;
    for (size_t index = 0; index < self->data->paths.size(); ++index) {
        const std::string& normalized = self->data->normalized_paths[index];
        if (!prefix.empty() && normalized.compare(0, prefix.size(), prefix) != 0) {
            continue;
        }
        if (!query.empty() && normalized.find(query) == std::string::npos) {
            continue;
        }
        matches.push_back(self->data->paths[index]);
        if (limit > 0 && matches.size() >= static_cast<size_t>(limit)) {
            break;
        }
    }
    return make_path_iterator(matches);
}

PyObject* directory_first_by_type(NativeDirectoryObject* self, PyObject* argument) {
    const long value = PyLong_AsLong(argument);
    if (value == -1 && PyErr_Occurred()) {
        return nullptr;
    }
    if (value < INT32_MIN || value > INT32_MAX) {
        PyErr_SetString(PyExc_OverflowError, "data_type must fit int32");
        return nullptr;
    }
    const int32_t data_type = static_cast<int32_t>(value);
    for (size_t index = 0; index < self->data->entries.size(); ++index) {
        if (self->data->entries[index].data_type == data_type) {
            return PyLong_FromSize_t(index);
        }
    }
    Py_RETURN_NONE;
}

PyObject* directory_validate(NativeDirectoryObject* self, PyObject* argument) {
    PyObject* sequence = PySequence_Fast(argument, "group_sizes must be a sequence");
    if (sequence == nullptr) {
        return nullptr;
    }
    const Py_ssize_t group_count_value = PySequence_Fast_GET_SIZE(sequence);
    std::vector<int64_t> group_sizes;
    try {
        group_sizes.reserve(static_cast<size_t>(group_count_value));
        PyObject** items = PySequence_Fast_ITEMS(sequence);
        for (Py_ssize_t index = 0; index < group_count_value; ++index) {
            const long long size = PyLong_AsLongLong(items[index]);
            if ((size == -1 && PyErr_Occurred()) || size <= 0 || size > INT32_MAX) {
                Py_DECREF(sequence);
                if (!PyErr_Occurred()) {
                    PyErr_SetString(PyExc_ValueError, "group size must be in 1..INT32_MAX");
                }
                return nullptr;
            }
            group_sizes.push_back(size);
        }
    } catch (const std::bad_alloc&) {
        Py_DECREF(sequence);
        PyErr_NoMemory();
        return nullptr;
    }
    Py_DECREF(sequence);

    size_t bad_index = SIZE_MAX;
    const char* reason = nullptr;
    Py_BEGIN_ALLOW_THREADS
    for (size_t index = 0; index < self->data->entries.size(); ++index) {
        const Entry& entry = self->data->entries[index];
        if (entry.chunk_index < 0 ||
            static_cast<size_t>(entry.chunk_index) >= group_sizes.size()) {
            bad_index = index;
            reason = "chunk index";
            break;
        }
        if (entry.data_offset < 0 || entry.data_size < 0 ||
            static_cast<int64_t>(entry.data_offset) + entry.data_size >
                group_sizes[static_cast<size_t>(entry.chunk_index)]) {
            bad_index = index;
            reason = "data range";
            break;
        }
    }
    Py_END_ALLOW_THREADS
    if (bad_index != SIZE_MAX) {
        PyErr_Format(
            PyExc_ValueError,
            "directory entry %zu has invalid %s",
            bad_index,
            reason
        );
        return nullptr;
    }
    Py_RETURN_NONE;
}

PyMethodDef directory_methods[] = {
    {"lookup", reinterpret_cast<PyCFunction>(directory_lookup), METH_O, nullptr},
    {"path_at", reinterpret_cast<PyCFunction>(directory_path_at), METH_O, nullptr},
    {"entry_at", reinterpret_cast<PyCFunction>(directory_entry_at), METH_O, nullptr},
    {"iter_prefix", reinterpret_cast<PyCFunction>(directory_iter_prefix), METH_O, nullptr},
    {"first_by_type", reinterpret_cast<PyCFunction>(directory_first_by_type), METH_O, nullptr},
    {"validate", reinterpret_cast<PyCFunction>(directory_validate), METH_O, nullptr},
    {
        "search",
        _PyCFunction_CAST(directory_search),
        METH_VARARGS | METH_KEYWORDS,
        nullptr
    },
    {nullptr, nullptr, 0, nullptr},
};

PySequenceMethods directory_sequence = {
    directory_length,
    nullptr,
    nullptr,
    nullptr,
    nullptr,
    nullptr,
    nullptr,
    nullptr,
    nullptr,
    nullptr,
};

PyObject* module_abi_version(PyObject*, PyObject*) {
    return PyLong_FromLong(kAbiVersion);
}

PyObject* module_decrypt_lcg(PyObject*, PyObject* args) {
    PyObject* input = nullptr;
    unsigned long long seed_value = 0;
    unsigned long long increment_value = 0;
    if (!PyArg_ParseTuple(args, "OKK:decrypt_lcg", &input, &seed_value, &increment_value)) {
        return nullptr;
    }
    if (seed_value > UINT32_MAX || increment_value > UINT32_MAX) {
        PyErr_SetString(PyExc_OverflowError, "seed and increment must fit uint32");
        return nullptr;
    }
    BufferView source;
    if (!source.acquire(input)) {
        return nullptr;
    }
    if (source.size() > static_cast<size_t>(PY_SSIZE_T_MAX)) {
        PyErr_SetString(PyExc_OverflowError, "input is too large");
        return nullptr;
    }
    PyObject* result = PyBytes_FromStringAndSize(nullptr, static_cast<Py_ssize_t>(source.size()));
    if (result == nullptr) {
        return nullptr;
    }
    auto* output = reinterpret_cast<uint8_t*>(PyBytes_AS_STRING(result));
    if (source.size() != 0) {
        std::memcpy(output, source.data(), source.size());
    }
    uint32_t state = static_cast<uint32_t>(seed_value);
    const uint32_t increment = static_cast<uint32_t>(increment_value);
    Py_BEGIN_ALLOW_THREADS
    for (size_t offset = 0; offset < source.size(); offset += 4) {
        const uint32_t s1 = kLcgMultiplier * state + increment;
        const uint32_t s2 = kLcgMultiplier * s1 + increment;
        state = s2;
        const uint32_t key = (s1 & 0xFFFF0000u) | ((s2 >> 16) & 0xFFFFu);
        const size_t remaining = std::min<size_t>(4, source.size() - offset);
        for (size_t index = 0; index < remaining; ++index) {
            output[offset + index] ^= static_cast<uint8_t>(key >> (index * 8));
        }
    }
    Py_END_ALLOW_THREADS
    return result;
}

PyObject* module_build_directory(PyObject*, PyObject* args) {
    PyObject* entry_object = nullptr;
    PyObject* utf8_object = nullptr;
    PyObject* utf16_object = nullptr;
    if (!PyArg_ParseTuple(
            args,
            "OOO:build_directory",
            &entry_object,
            &utf8_object,
            &utf16_object
        )) {
        return nullptr;
    }
    BufferView entry_table;
    BufferView utf8_pool;
    BufferView utf16_pool;
    if (!entry_table.acquire(entry_object) ||
        !utf8_pool.acquire(utf8_object) ||
        !utf16_pool.acquire(utf16_object)) {
        return nullptr;
    }
    if (entry_table.size() % kEntrySize != 0) {
        PyErr_SetString(PyExc_ValueError, "entry table size is not divisible by 24");
        return nullptr;
    }
    const size_t count = entry_table.size() / kEntrySize;
    auto directory = std::make_unique<DirectoryData>();
    try {
        directory->entries.reserve(count);
        directory->paths.reserve(count);
        for (size_t index = 0; index < count; ++index) {
            const uint8_t* row = entry_table.data() + index * kEntrySize;
            Entry entry;
            entry.name_offset = static_cast<int32_t>(load_u32(row));
            entry.path_offset = static_cast<int32_t>(load_u32(row + 4));
            entry.chunk_index = static_cast<int32_t>(load_u32(row + 8));
            entry.data_offset = static_cast<int32_t>(load_u32(row + 12));
            entry.data_size = static_cast<int32_t>(load_u32(row + 16));
            entry.data_type = static_cast<int32_t>(load_u32(row + 20));
            std::string folder = resolve_name(
                entry.path_offset,
                utf8_pool.data(),
                utf8_pool.size(),
                utf16_pool.data(),
                utf16_pool.size()
            );
            std::string name = resolve_name(
                entry.name_offset,
                utf8_pool.data(),
                utf8_pool.size(),
                utf16_pool.data(),
                utf16_pool.size()
            );
            std::replace(folder.begin(), folder.end(), '\\', '/');
            std::replace(name.begin(), name.end(), '\\', '/');
            while (!folder.empty() && folder.front() == '/') {
                folder.erase(folder.begin());
            }
            while (!folder.empty() && folder.back() == '/') {
                folder.pop_back();
            }
            directory->entries.push_back(entry);
            directory->paths.push_back(folder.empty() ? name : folder + "/" + name);
        }
        if (!build_lookup(*directory)) {
            return nullptr;
        }
    } catch (const std::bad_alloc&) {
        PyErr_NoMemory();
        return nullptr;
    }
    return reinterpret_cast<PyObject*>(new_directory(std::move(directory)));
}

PyObject* dump_directory_impl(NativeDirectoryObject* directory) {
    std::vector<uint8_t> output;
    try {
        size_t estimated = 20;
        for (const std::string& path : directory->data->paths) {
            if (path.size() > UINT32_MAX || estimated > SIZE_MAX - kEntrySize - 4 - path.size()) {
                PyErr_SetString(PyExc_OverflowError, "directory payload is too large");
                return nullptr;
            }
            estimated += kEntrySize + 4 + path.size();
        }
        output.reserve(estimated);
        output.insert(output.end(), std::begin(kDirectoryMagic), std::end(kDirectoryMagic));
        append_u32(output, kDirectorySchema);
        append_u64(output, directory->data->entries.size());
        for (size_t index = 0; index < directory->data->entries.size(); ++index) {
            const Entry& entry = directory->data->entries[index];
            append_u32(output, static_cast<uint32_t>(entry.name_offset));
            append_u32(output, static_cast<uint32_t>(entry.path_offset));
            append_u32(output, static_cast<uint32_t>(entry.chunk_index));
            append_u32(output, static_cast<uint32_t>(entry.data_offset));
            append_u32(output, static_cast<uint32_t>(entry.data_size));
            append_u32(output, static_cast<uint32_t>(entry.data_type));
            const std::string& path = directory->data->paths[index];
            append_u32(output, static_cast<uint32_t>(path.size()));
            output.insert(output.end(), path.begin(), path.end());
        }
    } catch (const std::bad_alloc&) {
        PyErr_NoMemory();
        return nullptr;
    }
    return PyBytes_FromStringAndSize(
        reinterpret_cast<const char*>(output.data()),
        static_cast<Py_ssize_t>(output.size())
    );
}

PyObject* module_dump_directory(PyObject*, PyObject* argument) {
    if (!PyObject_TypeCheck(argument, &NativeDirectoryType)) {
        PyErr_SetString(PyExc_TypeError, "directory must be NativeDirectory");
        return nullptr;
    }
    return dump_directory_impl(reinterpret_cast<NativeDirectoryObject*>(argument));
}

PyObject* module_load_directory(PyObject*, PyObject* argument) {
    BufferView blob;
    if (!blob.acquire(argument)) {
        return nullptr;
    }
    if (blob.size() > kMaxDirectoryBlob) {
        PyErr_SetString(PyExc_ValueError, "directory blob exceeds the size limit");
        return nullptr;
    }
    if (blob.size() < 20 || std::memcmp(blob.data(), kDirectoryMagic, 8) != 0) {
        PyErr_SetString(PyExc_ValueError, "directory blob magic mismatch");
        return nullptr;
    }
    if (load_u32(blob.data() + 8) != kDirectorySchema) {
        PyErr_SetString(PyExc_ValueError, "directory blob schema mismatch");
        return nullptr;
    }
    const uint64_t count64 = load_u64(blob.data() + 12);
    if (count64 > static_cast<uint64_t>(PY_SSIZE_T_MAX) ||
        count64 > (blob.size() - 20) / (kEntrySize + 4)) {
        PyErr_SetString(PyExc_ValueError, "directory blob count is invalid");
        return nullptr;
    }
    const size_t count = static_cast<size_t>(count64);
    auto directory = std::make_unique<DirectoryData>();
    try {
        directory->entries.reserve(count);
        directory->paths.reserve(count);
        size_t offset = 20;
        for (size_t index = 0; index < count; ++index) {
            if (offset > blob.size() || blob.size() - offset < kEntrySize + 4) {
                PyErr_SetString(PyExc_ValueError, "directory blob is truncated");
                return nullptr;
            }
            Entry entry;
            entry.name_offset = static_cast<int32_t>(load_u32(blob.data() + offset));
            entry.path_offset = static_cast<int32_t>(load_u32(blob.data() + offset + 4));
            entry.chunk_index = static_cast<int32_t>(load_u32(blob.data() + offset + 8));
            entry.data_offset = static_cast<int32_t>(load_u32(blob.data() + offset + 12));
            entry.data_size = static_cast<int32_t>(load_u32(blob.data() + offset + 16));
            entry.data_type = static_cast<int32_t>(load_u32(blob.data() + offset + 20));
            offset += kEntrySize;
            const uint32_t path_size = load_u32(blob.data() + offset);
            offset += 4;
            if (path_size > blob.size() - offset) {
                PyErr_SetString(PyExc_ValueError, "directory path exceeds blob bounds");
                return nullptr;
            }
            const char* path_data = reinterpret_cast<const char*>(blob.data() + offset);
            PyObject* decoded = PyUnicode_DecodeUTF8(path_data, path_size, "strict");
            if (decoded == nullptr) {
                return nullptr;
            }
            Py_DECREF(decoded);
            directory->entries.push_back(entry);
            directory->paths.emplace_back(path_data, path_size);
            offset += path_size;
        }
        if (offset != blob.size()) {
            PyErr_SetString(PyExc_ValueError, "directory blob has trailing data");
            return nullptr;
        }
        if (!build_lookup(*directory)) {
            return nullptr;
        }
    } catch (const std::bad_alloc&) {
        PyErr_NoMemory();
        return nullptr;
    }
    return reinterpret_cast<PyObject*>(new_directory(std::move(directory)));
}

PyObject* unicode_from_utf8_string(const std::string& value) {
    return PyUnicode_DecodeUTF8(
        value.data(),
        static_cast<Py_ssize_t>(value.size()),
        "strict"
    );
}

PyObject* module_decode_token_text(PyObject*, PyObject* args) {
    PyObject* data_object = nullptr;
    PyObject* utf8_object = nullptr;
    PyObject* utf16_object = nullptr;
    if (!PyArg_ParseTuple(
            args,
            "OOO:decode_token_text",
            &data_object,
            &utf8_object,
            &utf16_object
        )) {
        return nullptr;
    }
    BufferView data;
    BufferView utf8_pool;
    BufferView utf16_pool;
    if (!data.acquire(data_object) ||
        !utf8_pool.acquire(utf8_object) ||
        !utf16_pool.acquire(utf16_object)) {
        return nullptr;
    }
    if (data.size() % 5 != 0) {
        PyObject* text = PyUnicode_DecodeUTF8(
            reinterpret_cast<const char*>(data.data()),
            static_cast<Py_ssize_t>(data.size()),
            "strict"
        );
        if (text != nullptr) {
            return text;
        }
        PyErr_Clear();
    }
    PyObject* lines = PyList_New(0);
    if (lines == nullptr) {
        return nullptr;
    }
    for (size_t offset = 0; offset + 4 < data.size(); offset += 5) {
        const uint8_t token_type = data.data()[offset];
        const int32_t value = static_cast<int32_t>(load_u32(data.data() + offset + 1));
        PyObject* line = nullptr;
        if (token_type == 0) {
            line = PyUnicode_FromFormat("%d", value);
        } else if (token_type == 2) {
            float float_value = 0.0f;
            std::memcpy(&float_value, &value, sizeof(float_value));
            PyObject* number = PyFloat_FromDouble(static_cast<double>(float_value));
            if (number != nullptr) {
                line = PyObject_Repr(number);
                Py_DECREF(number);
            }
        } else {
            const std::string name = resolve_name(
                value,
                utf8_pool.data(),
                utf8_pool.size(),
                utf16_pool.data(),
                utf16_pool.size()
            );
            if (token_type == 3) {
                if (name.size() >= 2 && name.front() == '[' && name.back() == ']') {
                    line = unicode_from_utf8_string(name);
                } else {
                    line = unicode_from_utf8_string("[" + name + "]");
                }
            } else if (token_type == 5 || token_type == 7) {
                line = unicode_from_utf8_string(name);
            } else if (token_type == 6) {
                line = unicode_from_utf8_string("`" + name + "`");
            } else {
                line = PyUnicode_FromFormat(
                    "<token:%u:0x%08x>",
                    static_cast<unsigned int>(token_type),
                    static_cast<unsigned int>(value)
                );
            }
        }
        if (line == nullptr || PyList_Append(lines, line) != 0) {
            Py_XDECREF(line);
            Py_DECREF(lines);
            return nullptr;
        }
        Py_DECREF(line);
    }
    PyObject* separator = PyUnicode_FromString("\n");
    if (separator == nullptr) {
        Py_DECREF(lines);
        return nullptr;
    }
    PyObject* result = PyUnicode_Join(separator, lines);
    Py_DECREF(separator);
    Py_DECREF(lines);
    return result;
}

PyMethodDef module_methods[] = {
    {"abi_version", module_abi_version, METH_NOARGS, nullptr},
    {"decrypt_lcg", module_decrypt_lcg, METH_VARARGS, nullptr},
    {"build_directory", module_build_directory, METH_VARARGS, nullptr},
    {"load_directory", module_load_directory, METH_O, nullptr},
    {"dump_directory", module_dump_directory, METH_O, nullptr},
    {"decode_token_text", module_decode_token_text, METH_VARARGS, nullptr},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef module_definition = {
    PyModuleDef_HEAD_INIT,
    "usdof_pvf_native",
    nullptr,
    -1,
    module_methods,
    nullptr,
    nullptr,
    nullptr,
    nullptr,
};

}  // namespace

PyMODINIT_FUNC PyInit_usdof_pvf_native() {
    NativeDirectoryType.tp_name = "usdof_pvf_native.NativeDirectory";
    NativeDirectoryType.tp_basicsize = sizeof(NativeDirectoryObject);
    NativeDirectoryType.tp_itemsize = 0;
    NativeDirectoryType.tp_dealloc = reinterpret_cast<destructor>(directory_dealloc);
    NativeDirectoryType.tp_flags = Py_TPFLAGS_DEFAULT;
    NativeDirectoryType.tp_methods = directory_methods;
    NativeDirectoryType.tp_as_sequence = &directory_sequence;
    NativeDirectoryType.tp_new = nullptr;
    if (PyType_Ready(&NativeDirectoryType) < 0) {
        return nullptr;
    }
    PyObject* module = PyModule_Create(&module_definition);
    if (module == nullptr) {
        return nullptr;
    }
    Py_INCREF(&NativeDirectoryType);
    if (PyModule_AddObject(
            module,
            "NativeDirectory",
            reinterpret_cast<PyObject*>(&NativeDirectoryType)
        ) != 0) {
        Py_DECREF(&NativeDirectoryType);
        Py_DECREF(module);
        return nullptr;
    }
    return module;
}
