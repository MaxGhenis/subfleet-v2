// Subfleet: the app's own files (design §9, C-28.3).
//
// Everything the app keeps is under its Application Support and Caches
// directories for its bundle id, written atomically, 0600 in 0700 directories:
// the outbox journal, drafts, and images staged for `attachment.add`.
// Foundation, Darwin and CryptoKit only.

import CryptoKit
import Darwin
import Foundation

enum PersistenceError: Error, Equatable {
    case io(String)
    case tooLarge(bytes: Int)
    case notAnImage
}

/// Where this build keeps its files. The development build has its own bundle
/// id and so its own directories (D-21).
struct AppPaths: Equatable {
    let support: URL
    let caches: URL

    var outboxURL: URL { support.appendingPathComponent("outbox.json") }
    var draftsDirectory: URL { support.appendingPathComponent("drafts", isDirectory: true) }
    var stateURL: URL { support.appendingPathComponent("state.json") }
    var attachmentsDirectory: URL { caches.appendingPathComponent("attachments", isDirectory: true) }

    static func standard(flavor: BuildFlavor = .current, fileManager: FileManager = .default) -> AppPaths {
        let support = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? fileManager.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        let caches = fileManager.urls(for: .cachesDirectory, in: .userDomainMask).first
            ?? fileManager.homeDirectoryForCurrentUser.appendingPathComponent("Library/Caches")
        return AppPaths(support: support.appendingPathComponent(flavor.bundleIdentifier, isDirectory: true),
                        caches: caches.appendingPathComponent(flavor.bundleIdentifier, isDirectory: true))
    }

    /// Everything under one directory, for tests and probes.
    static func rooted(at root: URL) -> AppPaths {
        AppPaths(support: root.appendingPathComponent("support", isDirectory: true),
                 caches: root.appendingPathComponent("caches", isDirectory: true))
    }
}

/// Create `directory` (and parents) with mode 0700 for anything this creates.
func ensurePrivateDirectory(_ directory: URL) throws {
    var missing: [URL] = []
    var cursor = directory.standardizedFileURL
    while !FileManager.default.fileExists(atPath: cursor.path) {
        missing.append(cursor)
        let parent = cursor.deletingLastPathComponent()
        if parent.path == cursor.path { break }
        cursor = parent
    }
    for url in missing.reversed() {
        if mkdir(url.path, 0o700) != 0 && errno != EEXIST {
            throw PersistenceError.io("cannot create \(url.path): \(String(cString: strerror(errno)))")
        }
    }
}

/// Write-then-rename with fsync, mode 0600: a reader sees the old file or the new one.
func atomicWrite(_ data: Data, to url: URL) throws {
    let directory = url.deletingLastPathComponent()
    try ensurePrivateDirectory(directory)
    let temporary = directory.appendingPathComponent(".\(url.lastPathComponent).\(UUID().uuidString.prefix(8)).tmp")
    let fd = open(temporary.path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0o600)
    guard fd >= 0 else { throw PersistenceError.io("cannot write \(temporary.path): \(String(cString: strerror(errno)))") }
    var failure: String?
    data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
        var offset = 0
        while offset < raw.count, let base = raw.baseAddress {
            let written = write(fd, base + offset, raw.count - offset)
            if written < 0 {
                if errno == EINTR { continue }
                failure = String(cString: strerror(errno))
                return
            }
            offset += written
        }
    }
    if failure == nil && fsync(fd) != 0 { failure = String(cString: strerror(errno)) }
    close(fd)
    if let failure {
        unlink(temporary.path)
        throw PersistenceError.io("cannot write \(url.path): \(failure)")
    }
    guard rename(temporary.path, url.path) == 0 else {
        let reason = String(cString: strerror(errno))
        unlink(temporary.path)
        throw PersistenceError.io("cannot replace \(url.path): \(reason)")
    }
    let dfd = open(directory.path, O_RDONLY)
    if dfd >= 0 {
        fsync(dfd)
        close(dfd)
    }
}

func sha256Hex(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

// MARK: - Drafts

/// A composer's unsent state for one conversation (design §9): text, staged
/// images and settings. Deleted after the receipt of the message that used it.
struct Draft: Codable, Equatable {
    var text: String
    var attachments: [StagedAttachment]
    var settings: ConversationSettings?
    var updated_at: String
}

/// The draft after Esc took a steer back (C-24.9): its words ahead of whatever
/// the draft held, and its images staged again, so they survive leaving the
/// conversation and quitting the app, not only the composer on screen.
func recalledDraft(_ existing: Draft?, text: String, staged: [StagedAttachment], now: String) -> Draft {
    let held = existing?.text ?? ""
    let merged = held.isEmpty ? text : text.isEmpty ? held : text + "\n\n" + held
    var attachments = existing?.attachments ?? []
    for image in staged where !attachments.contains(image) { attachments.append(image) }
    return Draft(text: merged, attachments: attachments, settings: existing?.settings, updated_at: now)
}

final class DraftStore {
    let directory: URL

    init(directory: URL) {
        self.directory = directory
    }

    /// Conversation ids are `cv-…`; a new conversation's draft uses its create
    /// request id. Anything else is hashed into a safe file name.
    func fileURL(for key: String) -> URL {
        let safe = !key.isEmpty && key.count <= 120
            && key.unicodeScalars.allSatisfy { CharacterSet.alphanumerics.contains($0) || "-_.".unicodeScalars.contains($0) }
            && !key.hasPrefix(".")
        let name = safe ? key : sha256Hex(Data(key.utf8))
        return directory.appendingPathComponent(name + ".json")
    }

    func load(_ key: String) -> Draft? {
        guard let data = try? Data(contentsOf: fileURL(for: key)) else { return nil }
        return try? JSONDecoder().decode(Draft.self, from: data)
    }

    func save(_ draft: Draft, for key: String) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        try atomicWrite(try encoder.encode(draft), to: fileURL(for: key))
    }

    func delete(_ key: String) {
        unlink(fileURL(for: key).path)
    }
}

// MARK: - Staged images

/// An image the app wrote under its caches directory, ready for `attachment.add`.
struct StagedAttachment: Codable, Equatable, Hashable {
    var path: String
    var sha256: String
    var media_type: String
    var bytes: Int
}

enum AttachmentStager {
    /// attachments.MAX_BYTES.
    static let maxBytes = 20 * 1024 * 1024

    /// PNG, JPEG, GIF or WebP by magic bytes, as `attachment.add` checks them.
    static func mediaType(of data: Data) -> (mediaType: String, fileExtension: String)? {
        let head = [UInt8](data.prefix(16))
        func starts(_ magic: [UInt8]) -> Bool { head.count >= magic.count && Array(head[0..<magic.count]) == magic }
        if starts([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) { return ("image/png", "png") }
        if starts([0xFF, 0xD8, 0xFF]) { return ("image/jpeg", "jpg") }
        if starts(Array("GIF87a".utf8)) || starts(Array("GIF89a".utf8)) { return ("image/gif", "gif") }
        if head.count >= 12, Array(head[0..<4]) == Array("RIFF".utf8), Array(head[8..<12]) == Array("WEBP".utf8) {
            return ("image/webp", "webp")
        }
        return nil
    }

    /// Write `data` as `<sha256>.<ext>` (0600) in `directory` and describe it.
    static func stage(_ data: Data, in directory: URL) throws -> StagedAttachment {
        guard !data.isEmpty, data.count <= maxBytes else { throw PersistenceError.tooLarge(bytes: data.count) }
        guard let kind = mediaType(of: data) else { throw PersistenceError.notAnImage }
        let digest = sha256Hex(data)
        let url = directory.appendingPathComponent("\(digest).\(kind.fileExtension)")
        if (try? Data(contentsOf: url)).map(sha256Hex) != digest {
            try atomicWrite(data, to: url)
        }
        return StagedAttachment(path: url.path, sha256: digest, media_type: kind.mediaType, bytes: data.count)
    }
}
