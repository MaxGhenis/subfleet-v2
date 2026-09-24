// Subfleet: the app's only way to the daemon (design D-21, §12; C-29.1).
//
// It connects to `<SUBFLEET_HOME or ~/.subfleet>/daemon.sock` and nothing
// else, sends one request per connection, and waits 15 s for an answer, or
// `wait_s + 15` for a long poll. A development build refuses `~/.subfleet`.
// Foundation and Darwin only; calls block, so the UI runs them off the main
// thread (the async wrappers below do that).

import Darwin
import Foundation

// MARK: - Build flavour

/// The release app, or the development build (bundle id `.dev`, D-21).
enum BuildFlavor: String, Equatable {
    case release
    case development

    static let releaseBundleIdentifier = "org.maxghenis.subfleet"
    static let developmentBundleIdentifier = "org.maxghenis.subfleet.dev"

    /// `SUBFLEET_DEV_BUILD` (build.sh --dev) or the development bundle id.
    static var current: BuildFlavor {
        #if SUBFLEET_DEV_BUILD
        return .development
        #else
        return Bundle.main.bundleIdentifier == developmentBundleIdentifier ? .development : .release
        #endif
    }

    var bundleIdentifier: String {
        self == .development ? BuildFlavor.developmentBundleIdentifier : BuildFlavor.releaseBundleIdentifier
    }
}

// MARK: - Endpoint

/// The state root the app talks to, and the files it may read there (C-29.1).
struct DaemonEndpoint: Equatable {
    let root: URL

    var socketURL: URL { root.appendingPathComponent("daemon.sock") }
    var statusURL: URL { root.appendingPathComponent("status.json") }
    var catalogURL: URL { root.appendingPathComponent("catalog.json") }
}

enum EndpointResolution: Equatable {
    case ready(DaemonEndpoint)
    /// The development build was pointed at the installed daemon's state root.
    case refused(root: URL, reason: String)

    var endpoint: DaemonEndpoint? {
        if case .ready(let endpoint) = self { return endpoint }
        return nil
    }
}

/// `SUBFLEET_HOME` (with `~` expanded) or `~/.subfleet`, as the daemon and
/// `statusFileURL` read it.
func subfleetStateRoot(environment: [String: String], home: URL) -> URL {
    let override = environment["SUBFLEET_HOME"]?.trimmingCharacters(in: .whitespacesAndNewlines)
    if let override, !override.isEmpty {
        let path = override == "~" ? home.path
            : override.hasPrefix("~/") ? home.appendingPathComponent(String(override.dropFirst(2))).path
            : override
        return URL(fileURLWithPath: path, isDirectory: true)
    }
    return home.appendingPathComponent(".subfleet", isDirectory: true)
}

/// Whether two directories are the same place: the same inode when both exist,
/// otherwise the same standardized, symlink-resolved path, ignoring case (APFS
/// is case-insensitive by default, so `~/.Subfleet` is `~/.subfleet`).
func sameDirectory(_ a: URL, _ b: URL) -> Bool {
    var sa = stat(), sb = stat()
    if stat(a.path, &sa) == 0, stat(b.path, &sb) == 0 {
        return sa.st_dev == sb.st_dev && sa.st_ino == sb.st_ino
    }
    func canonical(_ url: URL) -> String {
        var path = url.standardizedFileURL.resolvingSymlinksInPath().path
        while path.count > 1 && path.hasSuffix("/") { path.removeLast() }
        return path.lowercased()
    }
    return canonical(a) == canonical(b)
}

/// D-21, C-29.4: the development build never connects to `~/.subfleet`.
func resolveDaemonEndpoint(
    environment: [String: String] = ProcessInfo.processInfo.environment,
    home: URL = FileManager.default.homeDirectoryForCurrentUser,
    flavor: BuildFlavor = .current
) -> EndpointResolution {
    let root = subfleetStateRoot(environment: environment, home: home)
    if flavor == .development {
        let installed = home.appendingPathComponent(".subfleet", isDirectory: true)
        if sameDirectory(root, installed) {
            let unset = (environment["SUBFLEET_HOME"] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            let reason = unset
                ? "The development build needs SUBFLEET_HOME set to a development state root; it never connects to ~/.subfleet."
                : "SUBFLEET_HOME names ~/.subfleet, the installed daemon's state root; the development build never connects to it."
            return .refused(root: root, reason: reason)
        }
    }
    return .ready(DaemonEndpoint(root: root))
}

// MARK: - Errors

enum DaemonClientError: Error, Equatable {
    /// The development build was pointed at `~/.subfleet`.
    case endpointRefused(String)
    /// Nothing is listening (no socket, connection refused): the daemon is down.
    case unavailable(String)
    /// No answer within the op's timeout; the request may or may not have landed.
    case timedOut(op: String, seconds: TimeInterval)
    /// The connection broke before a whole answer arrived.
    case transport(String)
    /// The answer was not the protocol's shape, or not the op's result shape.
    case malformed(String)
    /// The request line would exceed the daemon's 1 MiB request limit.
    case requestTooLarge(bytes: Int)
    /// `ok:false`.
    case daemon(DaemonError)

    var daemonError: DaemonError? {
        if case .daemon(let error) = self { return error }
        return nil
    }

    /// Whether the request may be sent again as it is: nothing says it failed on
    /// its merits (idempotent ops resend the same key, D-22).
    var isRetryable: Bool {
        switch self {
        case .unavailable, .timedOut, .transport, .malformed: return true
        case .daemon(let error): return error.isTransient
        case .endpointRefused, .requestTooLarge: return false
        }
    }

    var summary: String {
        switch self {
        case .endpointRefused(let reason): return reason
        case .unavailable(let reason): return reason
        case .timedOut(let op, let seconds): return "\(op) had no answer within \(Int(seconds)) s"
        case .transport(let reason): return reason
        case .malformed(let reason): return "unexpected answer: \(reason)"
        case .requestTooLarge(let bytes): return "the request is \(bytes) bytes; the daemon takes at most 1 MiB"
        case .daemon(let error): return error.message
        }
    }
}

// MARK: - Transport

/// One request line out, one response line back.
protocol DaemonTransport {
    func exchange(_ line: Data, timeout: TimeInterval) throws -> Data
}

/// AF_UNIX stream socket, one connection per request (C-16). The deadline
/// covers connecting, writing and reading; `poll` enforces it.
struct UnixSocketTransport: DaemonTransport {
    let path: String
    /// A response larger than this is refused rather than buffered without bound.
    var maxResponseBytes = 64 * 1024 * 1024

    func exchange(_ line: Data, timeout: TimeInterval) throws -> Data {
        let deadline = Date().addingTimeInterval(timeout)
        let fd = try connectSocket(deadline: deadline, timeout: timeout)
        defer { close(fd) }
        try writeAll(fd, line, deadline: deadline, timeout: timeout)
        return try readLine(fd, deadline: deadline, timeout: timeout)
    }

    private func remainingMillis(_ deadline: Date) -> Int32 {
        let left = deadline.timeIntervalSinceNow
        return left <= 0 ? 0 : Int32(min(left * 1000, Double(Int32.max)).rounded(.up))
    }

    private func wait(_ fd: Int32, for events: Int16, deadline: Date, timeout: TimeInterval) throws {
        while true {
            var pfd = pollfd(fd: fd, events: events, revents: 0)
            let millis = remainingMillis(deadline)
            if millis == 0 { throw DaemonClientError.timedOut(op: "", seconds: timeout) }
            let ready = poll(&pfd, 1, millis)
            if ready > 0 { return }
            if ready == 0 { throw DaemonClientError.timedOut(op: "", seconds: timeout) }
            if errno != EINTR { throw DaemonClientError.transport("poll failed: \(String(cString: strerror(errno)))") }
        }
    }

    private func connectSocket(deadline: Date, timeout: TimeInterval) throws -> Int32 {
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let capacity = MemoryLayout.size(ofValue: address.sun_path)
        let bytes = Array(path.utf8)
        guard bytes.count < capacity else {
            throw DaemonClientError.unavailable("The socket path is \(bytes.count) bytes; macOS allows \(capacity - 1): \(path)")
        }
        withUnsafeMutableBytes(of: &address.sun_path) { raw in
            raw.copyBytes(from: bytes)
            raw[bytes.count] = 0
        }
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw DaemonClientError.transport("socket failed: \(String(cString: strerror(errno)))") }
        var on: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &on, socklen_t(MemoryLayout<Int32>.size))
        _ = fcntl(fd, F_SETFD, FD_CLOEXEC)
        _ = fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK)
        let result = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        if result == 0 { return fd }
        let code = errno
        if code == EINPROGRESS || code == EAGAIN {
            do {
                try wait(fd, for: Int16(POLLOUT), deadline: deadline, timeout: timeout)
            } catch {
                close(fd)
                throw error
            }
            var failure: Int32 = 0
            var size = socklen_t(MemoryLayout<Int32>.size)
            getsockopt(fd, SOL_SOCKET, SO_ERROR, &failure, &size)
            if failure == 0 { return fd }
            close(fd)
            throw connectError(failure)
        }
        close(fd)
        throw connectError(code)
    }

    private func connectError(_ code: Int32) -> DaemonClientError {
        switch code {
        case ENOENT, ECONNREFUSED, ENOTDIR:
            return .unavailable("The Subfleet daemon is not running at \(path).")
        case EACCES, EPERM:
            return .unavailable("The daemon socket at \(path) is not accessible to this user.")
        default:
            return .unavailable("Could not connect to \(path): \(String(cString: strerror(code))).")
        }
    }

    private func writeAll(_ fd: Int32, _ data: Data, deadline: Date, timeout: TimeInterval) throws {
        var offset = 0
        try data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            guard let base = raw.baseAddress else { return }
            while offset < raw.count {
                let written = write(fd, base + offset, raw.count - offset)
                if written > 0 {
                    offset += written
                } else if written < 0 && (errno == EAGAIN || errno == EINTR) {
                    try wait(fd, for: Int16(POLLOUT), deadline: deadline, timeout: timeout)
                } else {
                    throw DaemonClientError.transport("write failed: \(String(cString: strerror(errno)))")
                }
            }
        }
    }

    private func readLine(_ fd: Int32, deadline: Date, timeout: TimeInterval) throws -> Data {
        var buffer = Data()
        var chunk = [UInt8](repeating: 0, count: 65536)
        while true {
            try wait(fd, for: Int16(POLLIN), deadline: deadline, timeout: timeout)
            let count = read(fd, &chunk, chunk.count)
            if count > 0 {
                let start = buffer.count
                buffer.append(contentsOf: chunk[0..<count])
                if let newline = buffer[start...].firstIndex(of: UInt8(ascii: "\n")) {
                    return buffer[buffer.startIndex..<newline]
                }
                if buffer.count > maxResponseBytes {
                    throw DaemonClientError.transport("the answer exceeded \(maxResponseBytes) bytes")
                }
            } else if count == 0 {
                throw DaemonClientError.transport("the daemon closed the connection without an answer")
            } else if errno != EAGAIN && errno != EINTR {
                throw DaemonClientError.transport("read failed: \(String(cString: strerror(errno)))")
            }
        }
    }
}

// MARK: - Client

/// Something that answers the conversation ops: the socket client, or a fake.
protocol DaemonCalling: AnyObject {
    func call<Args: Encodable, Result: Decodable>(_ op: DaemonOperation<Args, Result>, _ args: Args) throws -> Result
}

/// Safe to share across threads: each call opens its own connection. Set
/// `onExchange` before the first call.
final class DaemonClient: DaemonCalling, @unchecked Sendable {
    /// The daemon reads at most this many bytes per request line (daemon.py).
    static let maxRequestBytes = 1024 * 1024

    let transport: DaemonTransport
    /// The answer deadline; a long poll adds its `wait_s` (design §12: 15 s).
    var baseTimeout: TimeInterval = 15
    /// Every exchange, raw, for diagnostics and the frontend probes.
    var onExchange: ((_ op: String, _ request: Data, _ response: Data?) -> Void)?
    private let makeID: () -> String

    init(transport: DaemonTransport, makeID: @escaping () -> String = { "app-" + UUID().uuidString.lowercased() }) {
        self.transport = transport
        self.makeID = makeID
    }

    convenience init(endpoint: DaemonEndpoint) {
        self.init(transport: UnixSocketTransport(path: endpoint.socketURL.path))
    }

    /// A client for the resolved endpoint, or the refusal as an error.
    static func forCurrentEndpoint(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        home: URL = FileManager.default.homeDirectoryForCurrentUser,
        flavor: BuildFlavor = .current
    ) throws -> DaemonClient {
        switch resolveDaemonEndpoint(environment: environment, home: home, flavor: flavor) {
        case .ready(let endpoint): return DaemonClient(endpoint: endpoint)
        case .refused(_, let reason): throw DaemonClientError.endpointRefused(reason)
        }
    }

    /// The request line for an op, as it goes on the wire (newline included).
    static func requestLine<Args: Encodable>(op: String, id: String, args: Args) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        var data = try encoder.encode(RequestEnvelope(v: subfleetProtocolVersion, id: id, op: op, args: args))
        data.append(UInt8(ascii: "\n"))
        return data
    }

    /// Decode one response line for a request id.
    static func decodeResponse<Result: Decodable>(_ line: Data, id: String, op: String) throws -> Result {
        let decoder = JSONDecoder()
        let header: ResponseHeader
        do {
            header = try decoder.decode(ResponseHeader.self, from: line)
        } catch {
            throw DaemonClientError.malformed("\(op): not a protocol response (\(error))")
        }
        // A line the daemon could not parse is answered with id "" (daemon._connection).
        guard header.id == id || (header.id.isEmpty && !header.ok) else {
            throw DaemonClientError.malformed("\(op): answer for request \(header.id), expected \(id)")
        }
        guard header.ok else {
            throw DaemonClientError.daemon(header.error ?? DaemonError(code: 1, message: "the daemon refused without a reason", fix: nil))
        }
        do {
            return try decoder.decode(ResponseResult<Result>.self, from: line).result
        } catch {
            throw DaemonClientError.malformed("\(op): result does not match \(Result.self): \(error)")
        }
    }

    func call<Args: Encodable, Result: Decodable>(_ op: DaemonOperation<Args, Result>, _ args: Args) throws -> Result {
        let id = makeID()
        let line = try DaemonClient.requestLine(op: op.name, id: id, args: args)
        guard line.count <= DaemonClient.maxRequestBytes else {
            throw DaemonClientError.requestTooLarge(bytes: line.count)
        }
        let timeout = op.timeout(for: args, base: baseTimeout)
        let response: Data
        do {
            response = try transport.exchange(line, timeout: timeout)
        } catch DaemonClientError.timedOut {
            onExchange?(op.name, line, nil)
            throw DaemonClientError.timedOut(op: op.name, seconds: timeout)
        } catch {
            onExchange?(op.name, line, nil)
            throw error
        }
        onExchange?(op.name, line, response)
        return try DaemonClient.decodeResponse(response, id: id, op: op.name)
    }

    /// Run a blocking call on a background queue.
    func callAsync<Args: Encodable, Result: Decodable>(_ op: DaemonOperation<Args, Result>, _ args: Args) async throws -> Result {
        try await withCheckedThrowingContinuation { continuation in
            DaemonClient.queue.async {
                continuation.resume(with: Result_.init { try self.call(op, args) })
            }
        }
    }

    private typealias Result_ = Swift.Result
    private static let queue = DispatchQueue(label: "org.maxghenis.subfleet.daemon-client", attributes: .concurrent)
}

// MARK: - Availability

/// What the app can do with the daemon right now (C-25.1, C-29.2).
enum DaemonAvailability: Equatable {
    case unknown
    case ready(Capabilities)
    /// Not running, or not answering.
    case down(String)
    /// Running, but without the conversation ops, or another protocol or schema.
    case incompatible(String)
    /// The development build was pointed at `~/.subfleet`.
    case refused(String)

    var capabilities: Capabilities? {
        if case .ready(let capabilities) = self { return capabilities }
        return nil
    }

    var isReady: Bool { capabilities != nil }

    /// The banner a person sees, with what to do about it.
    var banner: (title: String, detail: String)? {
        switch self {
        case .unknown, .ready: return nil
        case .down(let detail):
            return ("The Subfleet daemon is not reachable", detail + " Start it with `subfleet daemon start`; drafts and queued messages are kept.")
        case .incompatible(let detail):
            return ("This daemon does not speak the conversation protocol", detail)
        case .refused(let detail):
            return ("This development build is not connected", detail)
        }
    }

    /// C-25.1: the conversation ops only after `capabilities` says so.
    static func judge(_ capabilities: Capabilities) -> DaemonAvailability {
        if capabilities.protocol != subfleetProtocolVersion {
            return .incompatible("The daemon speaks protocol \(capabilities.protocol); this app speaks \(subfleetProtocolVersion).")
        }
        if capabilities.conversation_schema != subfleetConversationSchema {
            return .incompatible("The daemon's conversation schema is \(capabilities.conversation_schema); this app knows \(subfleetConversationSchema). Install matching releases.")
        }
        if !capabilities.has(requiredDaemonCapability) {
            return .incompatible("The daemon does not offer \(requiredDaemonCapability).")
        }
        return .ready(capabilities)
    }

    /// `capabilities`, called at launch and on every reconnect.
    static func check(_ client: DaemonCalling) -> DaemonAvailability {
        do {
            return judge(try client.call(Ops.capabilities, NoArgs()))
        } catch let error as DaemonClientError {
            switch error {
            case .endpointRefused(let reason): return .refused(reason)
            case .daemon(let refusal) where refusal.isUnknownOp:
                return .incompatible("The daemon (\(refusal.message)) predates the conversation protocol; update Subfleet.")
            case .daemon(let refusal): return .incompatible(refusal.message)
            case .malformed(let reason): return .incompatible(reason)
            default: return .down(error.summary)
            }
        } catch {
            return .down("\(error)")
        }
    }
}
