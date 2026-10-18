import Foundation

/// A display parser only. It never evaluates expansions or executes a script.
enum ShellCommandPresentation {
    static func script(_ command: String) -> String {
        let argv = words(command, operators: false)
        guard let shell = argv.first, ["/bin/zsh", "/bin/bash", "/bin/sh", "zsh", "bash", "sh"].contains(shell) else { return command }
        if argv.count == 3, ["-c", "-lc"].contains(argv[1]) { return argv[2] }
        if argv.count == 4, argv[1] == "-l", argv[2] == "-c" { return argv[3] }
        return command
    }

    static func label(_ command: String) -> String {
        var tokens = words(script(command), operators: true)
        let separators: Set<String> = ["&&", "||", "|", ";", "\n", "&", ")"]
        // Only skip a prelude through its own separator. Looking for any later
        // && can hide the first substantive (and potentially destructive) command.
        while !tokens.isEmpty {
            if tokens.first == "(" { tokens.removeFirst(); continue }
            guard let end = tokens.firstIndex(where: { separators.contains($0) }) else { break }
            let head = Array(tokens[..<end]), separator = tokens[end]
            if head.first == "cd", ["&&", ";", "\n"].contains(separator) {
                tokens.removeFirst(end + 1)
            } else if head.first == "cd", separator == "||" {
                let rest = Array(tokens.dropFirst(end + 1))
                guard let exitEnd = rest.firstIndex(where: { separators.contains($0) }),
                      [";", "\n"].contains(rest[exitEnd]) else { break }
                let exit = Array(rest[..<exitEnd])
                guard exit == ["exit"] || (exit.count == 2 && exit[0] == "exit" && Int(exit[1]) != nil) else { break }
                tokens = Array(rest.dropFirst(exitEnd + 1))
            } else if [["set", "-e"], ["set", "-eu"], ["set", "-euo", "pipefail"], ["set", "-o", "pipefail"]].contains(head),
                      ["&&", ";", "\n"].contains(separator) {
                tokens.removeFirst(end + 1)
            } else { break }
        }
        while let first = tokens.first, first.contains("="), !first.hasPrefix("-") { tokens.removeFirst() }
        if tokens.first == "env" {
            tokens.removeFirst()
            while let first = tokens.first {
                if ["-u", "--unset"].contains(first), tokens.count > 1 { tokens.removeFirst(2) }
                else if first.hasPrefix("-") || first.contains("=") { tokens.removeFirst() }
                else { break }
            }
        }
        let head = Array(tokens.prefix { !separators.contains($0) })
        guard let executable = head.first else { return "Run a command" }
        let name = URL(fileURLWithPath: executable).lastPathComponent
        if name.hasPrefix("python") {
            let flags = head.dropFirst().prefix { $0.hasPrefix("-") && $0 != "-" }
            if flags.contains("-m"), let module = head.firstIndex(of: "-m"), head.indices.contains(module + 1) {
                return "\(name) -m \(head[module + 1])"
            }
            if flags.contains("-c") || head.contains("<<") || head.contains("<<-") { return "Run a Python script" }
        }
        let arguments = head.dropFirst().prefix { !["<", ">", "<<", "<<-", ">>"].contains($0) }.prefix(2)
        return ([name] + arguments).joined(separator: " ")
    }

    private static func words(_ text: String, operators: Bool) -> [String] {
        let chars = Array(text)
        var result: [String] = [], word = "", quote: Character?, started = false, index = 0
        func finish() {
            if started { result.append(word); word = ""; started = false }
        }
        while index < chars.count {
            let char = chars[index]
            if char == "\\", quote != "'", index + 1 < chars.count {
                let next = chars[index + 1]
                if quote == nil || ["\\", "\"", "$", "`", "\n"].contains(next) {
                    if next != "\n" { word.append(next); started = true }
                    index += 2
                    continue
                }
            }
            if let current = quote {
                if char == current { quote = nil } else { word.append(char) }
            } else if char == "'" || char == "\"" {
                quote = char; started = true
            } else if operators, "&|;<>\n()".contains(char) {
                finish()
                var op = String(char)
                if index + 1 < chars.count, chars[index + 1] == char, "&|<>".contains(char) {
                    op.append(char); index += 1
                    if op == "<<", index + 1 < chars.count, chars[index + 1] == "-" { op.append("-"); index += 1 }
                }
                result.append(op)
            } else if char.isWhitespace { finish() }
            else { word.append(char); started = true }
            index += 1
        }
        finish()
        return result
    }
}
