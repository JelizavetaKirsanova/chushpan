import CryptoKit
import Combine
import Foundation
import OSLog
import Security

enum MacBridgeError: LocalizedError {
    case invalidInvite
    case invalidAddress
    case certificateMismatch
    case untrustedCertificate
    case notPaired
    case pairingEnded
    case authorization(String)
    case timeout
    case protocolMismatch(String)
    case server(String)
    case transport

    var errorDescription: String? {
        switch self {
        case .invalidInvite: "В приглашении Mac нет одноразового кода сопряжения."
        case .invalidAddress: "Нужен корректный HTTPS-адрес сервера Чушпана."
        case .certificateMismatch: "Сертификат или код сопряжения не совпал с Mac. Отклони неизвестный запрос."
        case .untrustedCertificate: "Сертификат Mac не прошёл проверку TLS."
        case .notPaired: "Сначала подтверди сопряжение на Mac."
        case .pairingEnded: "Запрос сопряжения отклонён или истёк. Можно начать его снова."
        case .authorization(let message): "Mac отклонил авторизацию: \(message)"
        case .timeout: "Mac не ответил за отведённое время."
        case .protocolMismatch(let detail): "Ответ Mac несовместим с протоколом (\(detail)). Обнови Чушпан на обоих устройствах."
        case .server(let message): message
        case .transport: "Mac недоступен по защищённой сети. Команда не повторялась."
        }
    }
}

struct MacPairResponse: Decodable {
    var status: String
    var id: String?
    var code: String?
}

struct MacPairingResult {
    var code: String
    var deviceID: String
    var client: MacBridgeClient
}

struct MacCommandReceipt: Decodable {
    var status: String
    var id: String?
    var result: ReceiptDetails?
}

struct ReceiptDetails: Decodable {
    var message: String?
    var error: String?
    var resultStatus: String?
    var provider: String?
    var model: String?

    enum CodingKeys: String, CodingKey {
        case message, error, provider, model
        case resultStatus = "result_status"
    }
}

private final class BootstrapCertificateDelegate: NSObject, URLSessionTaskDelegate {
    private let host: String

    init(host: String) {
        self.host = host
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              challenge.protectionSpace.host == host,
              task.originalRequest?.url?.path == "/api/certificate",
              let trust = challenge.protectionSpace.serverTrust else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        // The pairing code independently binds this public certificate to the Mac UI.
        // Later requests use the returned CA as a TLS trust anchor.
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
}

private final class PinnedCertificateDelegate: NSObject, URLSessionDelegate {
    private let anchor: SecCertificate
    private let host: String

    init(certificate: SecCertificate, host: String) {
        self.anchor = certificate
        self.host = host
    }

    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              challenge.protectionSpace.host == host,
              let trust = challenge.protectionSpace.serverTrust else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        SecTrustSetPolicies(trust, SecPolicyCreateSSL(true, host as CFString))
        SecTrustSetAnchorCertificates(trust, [anchor] as CFArray)
        SecTrustSetAnchorCertificatesOnly(trust, true)
        var error: CFError?
        guard SecTrustEvaluateWithError(trust, &error) else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
}

final class MacBridgeClient {
    private static let logger = Logger(subsystem: "ee.chushpan.mobile.personal", category: "MacBridge")
    private static let privateKeyAccount = "mac-device-signing-key"
    private let origin: String
    private let baseURL: URL
    private let signer: P256.Signing.PrivateKey
    private let identity: String
    private let session: URLSession

    private init(baseURL: URL, signer: P256.Signing.PrivateKey, session: URLSession) {
        self.baseURL = baseURL
        self.origin = baseURL.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        self.signer = signer
        self.identity = Self.identity(for: signer.publicKey)
        self.session = session
    }

    static func pair(
        invitation: String,
        fingerprint: String,
        deviceName: String,
        kind: String
    ) async throws -> (MacBridgeClient, String, String) {
        guard let components = URLComponents(string: invitation),
              components.scheme?.lowercased() == "https",
              let host = components.host,
              components.user == nil,
              components.password == nil,
              let fragment = components.fragment,
              let nonce = URLComponents(string: "https://pair.invalid/?\(fragment)")?
                .queryItems?.first(where: { $0.name == "pair" })?.value,
              !nonce.isEmpty else { throw MacBridgeError.invalidInvite }
        var root = components
        root.fragment = nil
        root.query = nil
        root.path = ""
        guard let baseURL = root.url else { throw MacBridgeError.invalidAddress }
        var certificateURL = root
        certificateURL.path = "/api/certificate"
        guard let certificateURLValue = certificateURL.url else { throw MacBridgeError.invalidAddress }

        let bootstrapDelegate = BootstrapCertificateDelegate(host: host)
        let bootstrap = URLSession(configuration: .ephemeral, delegate: bootstrapDelegate, delegateQueue: nil)
        defer { bootstrap.finishTasksAndInvalidate() }
        let (certificateData, response): (Data, URLResponse)
        var certificateRequest = URLRequest(url: certificateURLValue)
        certificateRequest.timeoutInterval = 6
        do { (certificateData, response) = try await bootstrap.data(for: certificateRequest) }
        catch { throw Self.networkError(error) }
        guard (response as? HTTPURLResponse)?.statusCode == 200,
              let certificate = SecCertificateCreateWithData(nil, certificateData as CFData) else {
            throw MacBridgeError.untrustedCertificate
        }
        let expectedFingerprint = fingerprint.filter(\.isHexDigit).lowercased()
        let actualFingerprint = SHA256.hash(data: certificateData).map { String(format: "%02x", $0) }.joined()
        guard expectedFingerprint.count == 64,
              constantTimeEqual(expectedFingerprint, actualFingerprint) else {
            throw MacBridgeError.certificateMismatch
        }

        let signer: P256.Signing.PrivateKey
        let vault = KeychainVault()
        if let stored = try vault.read(Self.privateKeyAccount) {
            signer = try P256.Signing.PrivateKey(rawRepresentation: stored)
        } else {
            signer = P256.Signing.PrivateKey()
            try vault.write(signer.rawRepresentation, account: Self.privateKeyAccount)
        }
        let delegate = PinnedCertificateDelegate(certificate: certificate, host: host)
        let session = URLSession(configuration: .ephemeral, delegate: delegate, delegateQueue: nil)
        let client = MacBridgeClient(baseURL: baseURL, signer: signer, session: session)
        let pairResponse: MacPairResponse = try await client.request(
            "pair",
            value: [
                "invite": nonce,
                "key": Self.jwk(for: signer.publicKey),
                "name": String(deviceName.prefix(80)),
                "kind": kind,
            ],
            allowPairing: true
        )
        guard pairResponse.status == "pending", let code = pairResponse.code else {
            throw MacBridgeError.notPaired
        }
        return (client, code, client.identity)
    }

    static func pair(
        baseURL: String,
        deviceName: String,
        kind: String
    ) async throws -> (MacBridgeClient, String, String, String) {
        guard let components = URLComponents(string: baseURL),
              components.scheme?.lowercased() == "https",
              let host = components.host,
              components.user == nil,
              components.password == nil,
              let url = components.url,
              components.path.isEmpty || components.path == "/",
              components.query == nil,
              components.fragment == nil else { throw MacBridgeError.invalidAddress }
        var certificateURL = components
        certificateURL.path = "/api/certificate"
        guard let certificateURLValue = certificateURL.url else { throw MacBridgeError.invalidAddress }
        let bootstrapDelegate = BootstrapCertificateDelegate(host: host)
        let bootstrap = URLSession(configuration: .ephemeral, delegate: bootstrapDelegate, delegateQueue: nil)
        defer { bootstrap.finishTasksAndInvalidate() }
        let (certificateData, response): (Data, URLResponse)
        var certificateRequest = URLRequest(url: certificateURLValue)
        certificateRequest.timeoutInterval = 6
        do { (certificateData, response) = try await bootstrap.data(for: certificateRequest) }
        catch { throw Self.networkError(error) }
        guard (response as? HTTPURLResponse)?.statusCode == 200,
              let certificate = SecCertificateCreateWithData(nil, certificateData as CFData) else {
            throw MacBridgeError.untrustedCertificate
        }

        let fingerprint = SHA256.hash(data: certificateData)
            .map { String(format: "%02x", $0) }.joined()
        let vault = KeychainVault()
        let signer: P256.Signing.PrivateKey
        if let stored = try vault.read(Self.privateKeyAccount) {
            signer = try P256.Signing.PrivateKey(rawRepresentation: stored)
        } else {
            signer = P256.Signing.PrivateKey()
            try vault.write(signer.rawRepresentation, account: Self.privateKeyAccount)
        }
        let challenge = Self.base64URL(Data((UUID().uuidString + UUID().uuidString).utf8))
        let identity = Self.identity(for: signer.publicKey)
        let code = Self.pairingCode(challenge: challenge, identity: identity, fingerprint: fingerprint)
        let delegate = PinnedCertificateDelegate(certificate: certificate, host: host)
        let session = URLSession(configuration: .ephemeral, delegate: delegate, delegateQueue: nil)
        let client = MacBridgeClient(baseURL: url, signer: signer, session: session)
        let result: MacPairResponse = try await client.request(
            "pair",
            value: [
                "challenge": challenge,
                "key": Self.jwk(for: signer.publicKey),
                "name": String(deviceName.prefix(80)),
                "kind": kind,
            ],
            allowPairing: true
        )
        guard result.status == "pending", let serverCode = result.code else {
            client.close()
            throw MacBridgeError.notPaired
        }
        // A MITM cannot choose a code that also matches the code displayed by the Mac:
        // both independently derive it from this challenge, the public key and the Mac CA.
        guard constantTimeEqual(code, serverCode) else {
            client.close()
            throw MacBridgeError.certificateMismatch
        }
        return (client, code, client.identity, fingerprint)
    }

    static func restore(baseURL: String, fingerprint: String) async throws -> MacBridgeClient {
        guard let components = URLComponents(string: baseURL),
              components.scheme?.lowercased() == "https",
              let host = components.host,
              components.user == nil,
              components.password == nil,
              let url = components.url else { throw MacBridgeError.invalidAddress }
        var certificateURL = components
        certificateURL.path = "/api/certificate"
        certificateURL.query = nil
        certificateURL.fragment = nil
        guard let certificateURLValue = certificateURL.url else { throw MacBridgeError.invalidAddress }
        let bootstrapDelegate = BootstrapCertificateDelegate(host: host)
        let bootstrap = URLSession(configuration: .ephemeral, delegate: bootstrapDelegate, delegateQueue: nil)
        defer { bootstrap.finishTasksAndInvalidate() }
        let (data, response): (Data, URLResponse)
        var certificateRequest = URLRequest(url: certificateURLValue)
        certificateRequest.timeoutInterval = 6
        do { (data, response) = try await bootstrap.data(for: certificateRequest) }
        catch { throw Self.networkError(error) }
        guard (response as? HTTPURLResponse)?.statusCode == 200,
              let certificate = SecCertificateCreateWithData(nil, data as CFData) else {
            throw MacBridgeError.untrustedCertificate
        }
        let expected = fingerprint.filter(\.isHexDigit).lowercased()
        let actual = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        guard expected.count == 64, constantTimeEqual(expected, actual) else {
            throw MacBridgeError.certificateMismatch
        }
        guard let keyData = try KeychainVault().read(Self.privateKeyAccount) else {
            throw MacBridgeError.notPaired
        }
        let signer = try P256.Signing.PrivateKey(rawRepresentation: keyData)
        let delegate = PinnedCertificateDelegate(certificate: certificate, host: host)
        let session = URLSession(configuration: .ephemeral, delegate: delegate, delegateQueue: nil)
        return MacBridgeClient(baseURL: url, signer: signer, session: session)
    }

    func pairStatus() async throws -> MacPairResponse {
        try await request("pair_status", value: [:], allowPairing: true)
    }

    func status() async throws -> MacRemoteStatus {
        try await request("status", value: [:])
    }

    func synchronize() async throws -> MacSyncResponse {
        try await request("sync", value: [:])
    }

    func submit(operation: String, arguments: [String: Any]) async throws -> MacCommandReceipt {
        try await request(
            "command",
            value: [
                "id": UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased(),
                "operation": operation,
                "arguments": arguments,
                "expires": Date().timeIntervalSince1970 + 90,
            ]
        )
    }

    func result(id: String) async throws -> MacCommandReceipt {
        try await request("result", value: ["id": id])
    }

    func confirm(code: String) async throws -> MacCommandReceipt {
        try await submit(operation: "confirm", arguments: ["code": code])
    }

    func close() { session.invalidateAndCancel() }

    private func request<T: Decodable>(
        _ operation: String,
        value: [String: Any],
        allowPairing: Bool = false
    ) async throws -> T {
        let body: Data
        do { body = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) }
        catch { throw MacBridgeError.server("Некорректные данные команды.") }
        let timestamp = String(Int(Date().timeIntervalSince1970))
        let nonce = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let signedText = "/api/\(operation)\n\(timestamp)\n\(nonce)\n" + String(decoding: body, as: UTF8.self)
        guard let signature = try? signer.signature(for: Data(signedText.utf8)).rawRepresentation else {
            throw MacBridgeError.notPaired
        }
        let path = "/api/\(operation)"
        guard let url = URL(string: path, relativeTo: baseURL) else { throw MacBridgeError.invalidAddress }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 12
        request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(origin, forHTTPHeaderField: "Origin")
        request.setValue(identity, forHTTPHeaderField: "X-Device")
        request.setValue(timestamp, forHTTPHeaderField: "X-Time")
        request.setValue(nonce, forHTTPHeaderField: "X-Nonce")
        request.setValue(Self.base64URL(signature), forHTTPHeaderField: "X-Signature")
        let data: Data
        let response: URLResponse
        do { (data, response) = try await session.data(for: request) }
        catch let error as URLError where error.code == .timedOut { throw MacBridgeError.timeout }
        catch let error as URLError {
            switch error.code {
            case .cancelled: throw CancellationError()
            case .timedOut: throw MacBridgeError.timeout
            default: throw MacBridgeError.transport
            }
        }
        catch { throw MacBridgeError.transport }
        guard let http = response as? HTTPURLResponse else { throw MacBridgeError.transport }
        if !(200..<300).contains(http.statusCode) {
            if http.statusCode == 403, allowPairing, operation == "pair_status" {
                throw MacBridgeError.pairingEnded
            }
            let parsed = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            let message = (parsed?["error"] as? String)
                ?? (parsed?["detail"] as? String)
                ?? "Mac вернул HTTP \(http.statusCode)."
            let code = parsed?["code"] as? String
            Self.logger.error("Mac API rejected operation=\(operation, privacy: .public) http=\(http.statusCode) code=\((code ?? "unclassified"), privacy: .public)")
            if http.statusCode == 401 || code == "device_auth_failed" {
                throw MacBridgeError.authorization(message)
            }
            if http.statusCode == 403, code == "request_rejected" {
                throw MacBridgeError.protocolMismatch("сервер отклонил формат или Origin запроса")
            }
            if http.statusCode == 403 {
                throw MacBridgeError.authorization(message)
            }
            if [404, 406, 415, 422].contains(http.statusCode) {
                throw MacBridgeError.protocolMismatch("HTTP \(http.statusCode): \(message)")
            }
            throw MacBridgeError.server(message)
        }
        let contentType = http.value(forHTTPHeaderField: "Content-Type")?.lowercased() ?? ""
        Self.logger.info("Mac API response operation=\(operation, privacy: .public) http=\(http.statusCode) content_type=\(contentType, privacy: .public)")
        guard contentType.split(separator: ";", maxSplits: 1).first?.trimmingCharacters(in: .whitespaces)
                == "application/json" else {
            Self.logger.error("Mac API protocol mismatch operation=\(operation, privacy: .public) reason=content_type")
            throw MacBridgeError.protocolMismatch("\(operation): Content-Type \(contentType.isEmpty ? "отсутствует" : contentType)")
        }
        do { return try JSONDecoder.mobile.decode(T.self, from: data) }
        catch let error as DecodingError {
            let diagnostic = Self.decodingSummary(error)
            Self.logger.error("Mac API protocol mismatch operation=\(operation, privacy: .public) decoding=\(diagnostic, privacy: .public)")
            throw MacBridgeError.protocolMismatch("\(operation): \(diagnostic)")
        }
        catch {
            Self.logger.error("Mac API protocol mismatch operation=\(operation, privacy: .public) reason=invalid_json")
            throw MacBridgeError.protocolMismatch("\(operation): некорректный JSON")
        }
    }

    static func decodingSummary(_ error: DecodingError) -> String {
        func path(_ keys: [any CodingKey]) -> String {
            let value = keys.map(\.stringValue).joined(separator: ".")
            return value.isEmpty ? "$" : value
        }
        return switch error {
        case .keyNotFound(let key, let context):
            "нет поля \(path(context.codingPath + [key]))"
        case .typeMismatch(let type, let context):
            "неверный тип в \(path(context.codingPath)); ожидался \(type)"
        case .valueNotFound(let type, let context):
            "пустое значение в \(path(context.codingPath)); ожидался \(type)"
        case .dataCorrupted(let context):
            "повреждённое значение в \(path(context.codingPath))"
        @unknown default:
            "неизвестная ошибка декодирования"
        }
    }

    private static func jwk(for key: P256.Signing.PublicKey) -> [String: String] {
        let raw = Array(key.x963Representation)
        let x = Data(raw[1..<33])
        let y = Data(raw[33..<65])
        return ["kty": "EC", "crv": "P-256", "x": base64URL(x), "y": base64URL(y)]
    }

    private static func identity(for key: P256.Signing.PublicKey) -> String {
        let prefix = Data([
            0x30, 0x59, 0x30, 0x13, 0x06, 0x07, 0x2A, 0x86, 0x48, 0xCE, 0x3D,
            0x02, 0x01, 0x06, 0x08, 0x2A, 0x86, 0x48, 0xCE, 0x3D, 0x03, 0x01,
            0x07, 0x03, 0x42, 0x00,
        ])
        return SHA256.hash(data: prefix + key.x963Representation)
            .map { String(format: "%02x", $0) }.joined()
    }

    private static func base64URL(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    private static func constantTimeEqual(_ lhs: String, _ rhs: String) -> Bool {
        let left = Array(lhs.utf8), right = Array(rhs.utf8)
        guard left.count == right.count else { return false }
        return zip(left, right).reduce(UInt8(0)) { $0 | ($1.0 ^ $1.1) } == 0
    }

    private static func networkError(_ error: Error) -> Error {
        guard let urlError = error as? URLError else { return MacBridgeError.transport }
        switch urlError.code {
        case .timedOut: return MacBridgeError.timeout
        case .cancelled: return CancellationError()
        default: return MacBridgeError.transport
        }
    }

    static func pairingCode(challenge: String, identity: String, fingerprint: String) -> String {
        let digest = SHA256.hash(data: Data((challenge + identity + fingerprint).utf8))
        let prefix = digest.prefix(6).map { String(format: "%02x", $0) }.joined()
        return String(format: "%06llu", (UInt64(prefix, radix: 16) ?? 0) % 1_000_000)
    }
}

struct DiscoveredMac: Hashable, Identifiable {
    var id: String
    var name: String
    var host: String
    var port: Int
    var tailnetAddress: String?

    var localURL: String { "https://\(host):\(port)" }
    var remoteURL: String? {
        guard let tailnetAddress, !tailnetAddress.isEmpty else { return nil }
        return "https://\(tailnetAddress):\(port)"
    }
}

@MainActor
final class MacServiceBrowser: NSObject, ObservableObject, NetServiceBrowserDelegate, NetServiceDelegate {
    @Published private(set) var services: [DiscoveredMac] = []
    @Published private(set) var message = "Ищу Чушпан на локальной сети…"
    var onChange: (([DiscoveredMac]) -> Void)?
    var onMessage: ((String) -> Void)?

    private let browser = NetServiceBrowser()
    private var resolving: [String: NetService] = [:]
    private var resolved: [String: DiscoveredMac] = [:]
    private var isSearching = false
    private var searchTimeoutTask: Task<Void, Never>?

    func start() {
        guard !isSearching else { return }
        resolving.values.forEach { $0.stop() }
        resolving.removeAll()
        resolved.removeAll()
        services = []
        onChange?([])
        isSearching = true
        browser.delegate = self
        browser.searchForServices(ofType: "_chushpan._tcp.", inDomain: "local.")
        setMessage("Ищу Чушпан через Bonjour…")
        searchTimeoutTask?.cancel()
        searchTimeoutTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 8_000_000_000)
            guard let self, self.isSearching, self.services.isEmpty, !Task.isCancelled else { return }
            self.setMessage("Mac не найден. Проверь локальную сеть и Bonjour; гостевой или корпоративный Wi-Fi может блокировать обнаружение.")
        }
    }

    func stop() {
        guard isSearching else { return }
        browser.stop()
        searchTimeoutTask?.cancel()
        searchTimeoutTask = nil
        resolving.values.forEach { $0.stop() }
        resolving.removeAll()
        isSearching = false
    }

    func netServiceBrowser(_ browser: NetServiceBrowser, didFind service: NetService, moreComing: Bool) {
        let key = service.name + "|" + service.domain
        resolving[key] = service
        service.delegate = self
        service.resolve(withTimeout: 6)
    }

    func netServiceBrowser(_ browser: NetServiceBrowser, didRemove service: NetService, moreComing: Bool) {
        let key = service.name + "|" + service.domain
        resolving.removeValue(forKey: key)?.stop()
        resolved.removeValue(forKey: key)
        publishServices()
    }

    func netServiceBrowser(_ browser: NetServiceBrowser, didNotSearch errorDict: [String: NSNumber]) {
        searchTimeoutTask?.cancel()
        searchTimeoutTask = nil
        isSearching = false
        setMessage("Не удалось искать Mac через Bonjour. Разреши Чушпану доступ к локальной сети в настройках iPhone/iPad.")
    }

    func netServiceDidResolveAddress(_ sender: NetService) {
        let key = sender.name + "|" + sender.domain
        guard let host = sender.hostName?.trimmingCharacters(in: CharacterSet(charactersIn: ".")),
              !host.isEmpty,
              (1...65535).contains(sender.port) else { return }
        let txt = NetService.dictionary(fromTXTRecord: sender.txtRecordData() ?? Data())
        let tailnet = txt["tailscale"].flatMap { String(data: $0, encoding: .utf8) }
            .flatMap(Self.validatedTailnetAddress)
        resolved[key] = DiscoveredMac(
            id: key,
            name: sender.name,
            host: host,
            port: sender.port,
            tailnetAddress: tailnet
        )
        resolving.removeValue(forKey: key)
        publishServices()
    }

    func netService(_ sender: NetService, didNotResolve errorDict: [String: NSNumber]) {
        resolving.removeValue(forKey: sender.name + "|" + sender.domain)
        if resolved.isEmpty {
            setMessage("Mac найден, но адрес Bonjour не разрешился. Проверь Wi-Fi и сетевую изоляцию.")
        }
    }

    private func publishServices() {
        searchTimeoutTask?.cancel()
        searchTimeoutTask = nil
        services = resolved.values.sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
        setMessage(services.isEmpty
            ? "Mac не найден. Проверь, что локальный сервер работает и оба устройства в одной сети; некоторые Wi-Fi блокируют Bonjour."
            : "Найдено устройств Mac: \(services.count).")
        onChange?(services)
    }

    private func setMessage(_ value: String) {
        message = value
        onMessage?(value)
    }

    private static func validatedTailnetAddress(_ value: String) -> String? {
        let octets = value.split(separator: ".").compactMap { UInt8($0) }
        guard octets.count == 4, octets[0] == 100, (64...127).contains(Int(octets[1])) else {
            return nil
        }
        return value
    }
}

enum CoreAPIError: LocalizedError {
    case invalidInvitation(String)
    case certificateMismatch
    case authorization(String)
    case protocolMismatch(String)
    case unavailable(String)
    case server(String)

    var errorDescription: String? {
        switch self {
        case .invalidInvitation(let detail): "Приглашение Core API не подходит: \(detail)"
        case .certificateMismatch: "Отпечаток Core API не совпадает с приглашением. Сопряжение остановлено."
        case .authorization(let detail): "Core API отклонил авторизацию: \(detail)"
        case .protocolMismatch(let detail): "Core API вернул несовместимый ответ: \(detail)"
        case .unavailable(let detail): "Core API недоступен: \(detail)"
        case .server(let detail): detail
        }
    }
}

struct CoreAPIInvitation: Decodable {
    var code: String
    var host: String
    var tlsFingerprint: String
    var scopes: [String]

    enum CodingKeys: String, CodingKey {
        case code, host, scopes
        case tlsFingerprint = "tls_fingerprint"
    }
}

struct CoreAPIStatus: Decodable {
    struct LLM: Decodable {
        var provider: String
        var configured: Bool
    }

    var llm: LLM
}

private final class CorePinnedCertificateDelegate: NSObject, URLSessionDelegate {
    private let host: String
    private let fingerprint: String

    init(host: String, fingerprint: String) {
        self.host = host
        self.fingerprint = fingerprint.lowercased()
    }

    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              challenge.protectionSpace.host == host,
              let trust = challenge.protectionSpace.serverTrust,
              let certificate = SecTrustGetCertificateAtIndex(trust, 0),
              let der = SecCertificateCopyData(certificate) as Data? else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        let actual = SHA256.hash(data: der).map { String(format: "%02x", $0) }.joined()
        guard CoreAPIClient.constantTimeEqual(actual, fingerprint) else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
}

final class CoreAPIClient {
    private static let privateKeyAccount = "core-api-device-signing-key"
    private let host: String
    private let fingerprint: String
    private let signer: P256.Signing.PrivateKey
    private let deviceID: String
    private let session: URLSession

    private init(host: String, fingerprint: String, signer: P256.Signing.PrivateKey) {
        self.host = host
        self.fingerprint = fingerprint.lowercased()
        self.signer = signer
        deviceID = Self.identity(for: signer.publicKey)
        let delegate = CorePinnedCertificateDelegate(host: host, fingerprint: fingerprint)
        session = URLSession(configuration: .ephemeral, delegate: delegate, delegateQueue: nil)
    }

    static func pair(invitationJSON: String) async throws -> CoreAPIClient {
        let invite = try validatedInvitation(invitationJSON)
        let vault = KeychainVault()
        let signer: P256.Signing.PrivateKey
        if let stored = try vault.read(privateKeyAccount) {
            signer = try P256.Signing.PrivateKey(rawRepresentation: stored)
        } else {
            signer = P256.Signing.PrivateKey()
            try vault.write(signer.rawRepresentation, account: privateKeyAccount)
        }
        let client = CoreAPIClient(host: invite.host, fingerprint: invite.tlsFingerprint, signer: signer)
        _ = try await client.get("/health", authenticated: false, responseType: CoreHealth.self)
        let pairBody: [String: Any] = ["code": invite.code, "public_jwk": publicJWK(signer.publicKey)]
        let paired: CorePairedDevice = try await client.post("/v1/pair", body: pairBody, authenticated: false)
        guard paired.deviceID == client.deviceID, paired.scopes == ["llm.chat"] else {
            throw CoreAPIError.protocolMismatch("сервер выдал неожиданные разрешения или идентификатор")
        }
        UserDefaults.standard.set(invite.host, forKey: "mobile.coreAPIHost")
        UserDefaults.standard.set(invite.tlsFingerprint.lowercased(), forKey: "mobile.coreAPIFingerprint")
        UserDefaults.standard.set(paired.deviceID, forKey: "mobile.coreAPIDeviceID")
        UserDefaults.standard.set(true, forKey: "mobile.coreAPITrusted")
        return client
    }

    static func restore() throws -> CoreAPIClient? {
        let defaults = UserDefaults.standard
        guard defaults.bool(forKey: "mobile.coreAPITrusted"),
              let host = defaults.string(forKey: "mobile.coreAPIHost"), validTailnetIPv4(host),
              let fingerprint = defaults.string(forKey: "mobile.coreAPIFingerprint"),
              fingerprint.range(of: "^[A-Fa-f0-9]{64}$", options: .regularExpression) != nil,
              let keyData = try KeychainVault().read(privateKeyAccount) else { return nil }
        let signer = try P256.Signing.PrivateKey(rawRepresentation: keyData)
        let client = CoreAPIClient(host: host, fingerprint: fingerprint, signer: signer)
        guard defaults.string(forKey: "mobile.coreAPIDeviceID") == client.deviceID else {
            throw CoreAPIError.authorization("закрытый ключ не соответствует сохранённому устройству")
        }
        return client
    }

    func status() async throws -> CoreAPIStatus {
        try await get("/v1/core/status", responseType: CoreAPIStatus.self)
    }

    func chat(messages: [[String: String]]) async throws -> String {
        guard !messages.isEmpty, messages.count <= 32,
              messages.allSatisfy({ ($0["content"]?.count ?? 0) <= 20_000 }),
              messages.compactMap({ $0["content"] }).joined().count <= 60_000 else {
            throw CoreAPIError.protocolMismatch("диалог превышает допустимый размер")
        }
        let payload: [String: Any] = [
            "request_id": UUID().uuidString.lowercased(),
            "messages": messages.map { ["role": $0["role"] ?? "user", "content": $0["content"] ?? ""] },
            "tools": [],
        ]
        let body = try Self.jsonData(payload)
        var request = signedRequest(path: "/v1/chat", method: "POST", body: body)
        request.timeoutInterval = 125
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let (bytes, response): (URLSession.AsyncBytes, URLResponse)
        do { (bytes, response) = try await session.bytes(for: request) }
        catch let error as URLError where error.code == .timedOut {
            throw CoreAPIError.unavailable("истёк тайм-аут запроса GPT")
        } catch let error as URLError where error.code == .cancelled { throw CancellationError() }
        catch { throw CoreAPIError.unavailable("ошибка сетевого запроса GPT") }
        try validate(response, expectedContentType: "text/event-stream")

        var eventName = ""
        var answer = ""
        var sawDone = false
        for try await line in bytes.lines {
            if line.isEmpty {
                eventName = ""
                continue
            }
            if line.hasPrefix("event:") { eventName = line.dropFirst(6).trimmingCharacters(in: .whitespaces) }
            else if line.hasPrefix("data:") {
                let data = String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces)
                switch eventName {
                case "token":
                    guard let event = try? JSONDecoder().decode(CoreTokenEvent.self, from: Data(data.utf8)),
                          answer.count + event.text.count <= 80_000 else {
                        throw CoreAPIError.protocolMismatch("событие token повреждено или слишком велико")
                    }
                    answer += event.text
                case "error":
                    let detail = (try? JSONDecoder().decode(CoreErrorEvent.self, from: Data(data.utf8)))?.detail
                        ?? "сервер вернул неразбираемую ошибку"
                    throw CoreAPIError.server(detail)
                case "done": sawDone = true
                case "tool_calls":
                    throw CoreAPIError.protocolMismatch("Core API запросил инструмент; прямой маршрут не разрешает команды Mac")
                default: break
                }
            }
        }
        guard sawDone else { throw CoreAPIError.unavailable("поток GPT завершился без события done") }
        return answer
    }

    func close() { session.invalidateAndCancel() }

    private func get<T: Decodable>(_ path: String, authenticated: Bool = true, responseType: T.Type) async throws -> T {
        var request = signedRequest(path: path, method: "GET", body: Data(), authenticated: authenticated)
        request.timeoutInterval = 10
        let (data, response) = try await fetch(request)
        try validate(response, expectedContentType: "application/json", data: data)
        do { return try JSONDecoder().decode(T.self, from: data) }
        catch { throw CoreAPIError.protocolMismatch("ответ JSON не соответствует схеме \(path)") }
    }

    private func post<T: Decodable>(_ path: String, body: [String: Any], authenticated: Bool) async throws -> T {
        var request = signedRequest(path: path, method: "POST", body: try Self.jsonData(body), authenticated: authenticated)
        request.timeoutInterval = 12
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await fetch(request)
        try validate(response, expectedContentType: "application/json", data: data)
        do { return try JSONDecoder().decode(T.self, from: data) }
        catch { throw CoreAPIError.protocolMismatch("ответ сопряжения имеет неизвестный формат") }
    }

    private func fetch(_ request: URLRequest) async throws -> (Data, URLResponse) {
        do { return try await session.data(for: request) }
        catch let error as URLError where error.code == .timedOut { throw CoreAPIError.unavailable("истёк тайм-аут") }
        catch let error as URLError where error.code == .cancelled { throw CancellationError() }
        catch { throw CoreAPIError.unavailable("сервер недоступен по Tailscale") }
    }

    private func validate(_ response: URLResponse, expectedContentType: String, data: Data? = nil) throws {
        guard let http = response as? HTTPURLResponse else {
            throw CoreAPIError.protocolMismatch("ответ не является HTTP")
        }
        if http.statusCode == 401 || http.statusCode == 403 {
            let object = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            throw CoreAPIError.authorization(object?["detail"] as? String ?? "HTTP \(http.statusCode)")
        }
        guard (200..<300).contains(http.statusCode) else {
            let object = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            throw CoreAPIError.server((object?["detail"] as? String) ?? "Сервер вернул HTTP \(http.statusCode).")
        }
        let contentType = http.value(forHTTPHeaderField: "Content-Type")?.lowercased() ?? ""
        guard contentType.hasPrefix(expectedContentType) else {
            throw CoreAPIError.protocolMismatch("ожидался Content-Type \(expectedContentType), получен \(contentType.isEmpty ? "пустой" : contentType)")
        }
    }

    private func signedRequest(path: String, method: String, body: Data, authenticated: Bool = true) -> URLRequest {
        var request = URLRequest(url: URL(string: "https://\(host):8765\(path)")!)
        request.httpMethod = method
        request.httpBody = method == "GET" ? nil : body
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        guard authenticated else { return request }
        let timestamp = String(Int(Date().timeIntervalSince1970))
        let nonce = Self.base64URL(Data(UUID().uuidString.lowercased().utf8))
        let payload = Self.requestPayload(method: method, target: path, timestamp: timestamp, nonce: nonce, body: body)
        let signature = (try? signer.signature(for: payload).rawRepresentation) ?? Data()
        request.setValue(deviceID, forHTTPHeaderField: "X-Chushpan-Device")
        request.setValue(timestamp, forHTTPHeaderField: "X-Chushpan-Timestamp")
        request.setValue(nonce, forHTTPHeaderField: "X-Chushpan-Nonce")
        request.setValue(Self.base64URL(signature), forHTTPHeaderField: "X-Chushpan-Signature")
        return request
    }

    private static func jsonData(_ value: [String: Any]) throws -> Data {
        do { return try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .fragmentsAllowed]) }
        catch { throw CoreAPIError.protocolMismatch("не удалось сформировать JSON запроса") }
    }

    static func validatedInvitation(_ raw: String) throws -> CoreAPIInvitation {
        guard let data = raw.data(using: .utf8),
              let invite = try? JSONDecoder().decode(CoreAPIInvitation.self, from: data),
              invite.code.range(of: "^[A-Za-z0-9_-]{32,64}$", options: .regularExpression) != nil,
              validTailnetIPv4(invite.host),
              invite.tlsFingerprint.range(of: "^[A-Fa-f0-9]{64}$", options: .regularExpression) != nil,
              Set(invite.scopes) == ["llm.chat"], invite.scopes.count == 1 else {
            throw CoreAPIError.invalidInvitation("нужны адрес Tailscale, SHA-256 отпечаток и только разрешение llm.chat")
        }
        return invite
    }

    static func requestPayload(method: String, target: String, timestamp: String, nonce: String, body: Data) -> Data {
        let hash = SHA256.hash(data: body).map { String(format: "%02x", $0) }.joined()
        return Data("v1\n\(timestamp)\n\(nonce)\n\(method.uppercased())\n\(target)\n\(hash)".utf8)
    }

    static func validTailnetIPv4(_ value: String) -> Bool {
        let octets = value.split(separator: ".").compactMap { UInt8($0) }
        return octets.count == 4 && octets[0] == 100 && (64...127).contains(Int(octets[1]))
    }

    private static func publicJWK(_ key: P256.Signing.PublicKey) -> [String: String] {
        let raw = Array(key.x963Representation)
        return ["kty": "EC", "crv": "P-256", "x": base64URL(Data(raw[1..<33])), "y": base64URL(Data(raw[33..<65]))]
    }

    private static func identity(for key: P256.Signing.PublicKey) -> String {
        let prefix = Data([0x30,0x59,0x30,0x13,0x06,0x07,0x2A,0x86,0x48,0xCE,0x3D,0x02,0x01,0x06,0x08,0x2A,0x86,0x48,0xCE,0x3D,0x03,0x01,0x07,0x03,0x42,0x00])
        return SHA256.hash(data: prefix + key.x963Representation).map { String(format: "%02x", $0) }.joined()
    }

    private static func base64URL(_ data: Data) -> String {
        data.base64EncodedString().replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
    }

    static func constantTimeEqual(_ lhs: String, _ rhs: String) -> Bool {
        let left = Array(lhs.utf8), right = Array(rhs.utf8)
        guard left.count == right.count else { return false }
        return zip(left, right).reduce(UInt8(0)) { $0 | ($1.0 ^ $1.1) } == 0
    }
}

private struct CoreHealth: Decodable {
    var status: String
    var service: String
}

private struct CorePairedDevice: Decodable {
    var deviceID: String
    var scopes: [String]

    enum CodingKeys: String, CodingKey {
        case scopes
        case deviceID = "device_id"
    }
}

private struct CoreTokenEvent: Decodable { var text: String }
private struct CoreErrorEvent: Decodable { var detail: String }

