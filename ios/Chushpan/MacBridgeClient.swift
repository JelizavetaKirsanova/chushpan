import CryptoKit
import Combine
import Foundation
import Security

enum MacBridgeError: LocalizedError {
    case invalidInvite
    case invalidAddress
    case certificateMismatch
    case untrustedCertificate
    case notPaired
    case pairingEnded
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

    enum CodingKeys: String, CodingKey {
        case message, error
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
        do { (certificateData, response) = try await bootstrap.data(from: certificateURLValue) }
        catch { throw MacBridgeError.transport }
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
        do { (certificateData, response) = try await bootstrap.data(from: certificateURLValue) }
        catch { throw MacBridgeError.transport }
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
        do { (data, response) = try await bootstrap.data(from: certificateURLValue) }
        catch { throw MacBridgeError.transport }
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
        catch { throw MacBridgeError.transport }
        guard let http = response as? HTTPURLResponse else { throw MacBridgeError.transport }
        if !(200..<300).contains(http.statusCode) {
            if http.statusCode == 403, allowPairing, operation == "pair_status" {
                throw MacBridgeError.pairingEnded
            }
            if http.statusCode == 403, !allowPairing { throw MacBridgeError.notPaired }
            let parsed = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            let message = parsed?["error"] as? String ?? "Mac вернул HTTP \(http.statusCode)."
            throw MacBridgeError.server(message)
        }
        do { return try JSONDecoder.mobile.decode(T.self, from: data) }
        catch { throw MacBridgeError.server("Ответ защищённого сервиса Mac имеет неизвестный формат.") }
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

