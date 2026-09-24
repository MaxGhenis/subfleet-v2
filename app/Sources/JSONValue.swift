// Subfleet: any JSON value, kept exactly as the daemon sent it.
//
// Approval display fields, masked approval requests, served facts, limits and
// event data are open-ended objects on the wire. They are decoded into this
// type so nothing the daemon shows a person is dropped by a decoder that did
// not know a key (C-27.1: every field that changes what is granted is shown).

import Foundation

enum JSONValue: Codable, Equatable, Hashable {
    case null
    case bool(Bool)
    case int(Int64)
    case double(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Int64.self) {
            self = .int(value)
        } else if let value = try? container.decode(Double.self) {
            self = .double(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "not a JSON value")
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case .bool(let value): try container.encode(value)
        case .int(let value): try container.encode(value)
        case .double(let value): try container.encode(value)
        case .string(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        }
    }

    // MARK: Access

    subscript(key: String) -> JSONValue? {
        if case .object(let object) = self { return object[key] }
        return nil
    }

    var isNull: Bool { self == .null }

    var string: String? {
        if case .string(let value) = self { return value }
        return nil
    }

    var bool: Bool? {
        if case .bool(let value) = self { return value }
        return nil
    }

    var int: Int? {
        switch self {
        case .int(let value): return Int(exactly: value)
        case .double(let value): return value.rounded() == value && value.isFinite ? Int(exactly: value) : nil
        default: return nil
        }
    }

    var double: Double? {
        switch self {
        case .int(let value): return Double(value)
        case .double(let value): return value
        default: return nil
        }
    }

    var array: [JSONValue]? {
        if case .array(let value) = self { return value }
        return nil
    }

    var object: [String: JSONValue]? {
        if case .object(let value) = self { return value }
        return nil
    }

    /// A value as a person reads it: strings as they are, anything else as
    /// compact JSON with sorted keys.
    var displayText: String {
        switch self {
        case .null: return ""
        case .string(let value): return value
        case .bool(let value): return value ? "true" : "false"
        case .int(let value): return String(value)
        case .double(let value):
            return value.rounded() == value && abs(value) < 1e15 ? String(Int64(value)) : String(value)
        case .array, .object:
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
            guard let data = try? encoder.encode(self), let text = String(data: data, encoding: .utf8) else { return "" }
            return text
        }
    }

    // MARK: Conversion

    static func from<T: Encodable>(_ value: T) throws -> JSONValue {
        try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(value))
    }

    func decode<T: Decodable>(_ type: T.Type) throws -> T {
        try JSONDecoder().decode(T.self, from: JSONEncoder().encode(self))
    }

    static func parse(_ data: Data) throws -> JSONValue {
        try JSONDecoder().decode(JSONValue.self, from: data)
    }
}

/// An open object whose keys the daemon may extend: known keys are read by
/// name, every key is kept and re-encoded unchanged.
protocol JSONObjectBacked: Codable, Equatable {
    var fields: [String: JSONValue] { get }
    init(fields: [String: JSONValue])
}

extension JSONObjectBacked {
    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self.init(fields: [:])
        } else {
            self.init(fields: try container.decode([String: JSONValue].self))
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(fields)
    }

    func string(_ key: String) -> String? { fields[key]?.string }
    func bool(_ key: String) -> Bool? { fields[key]?.bool }

    /// Keys whose value is present (not null), sorted.
    var presentKeys: [String] { fields.filter { !$0.value.isNull }.map(\.key).sorted() }
}
