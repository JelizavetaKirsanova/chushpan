import CryptoKit
import Foundation
import Security

enum MacBridgeError: LocalizedError {
    case invalidInvite
    case invalidAddress
    case certificateMismatch
    case untrustedCertificate
    case notPaired
    case server(String)
    case transport

    var errorDescription: String? {
        switch self {
        case .invalidInvite: "В приглашении Mac нет одноразового кода сопряжения."
        case .invalidAddress: "Нужен HTTPS-адрес локального Mac из приглашения Чушпана."
        case .certificateMismatch: "Отпечаток TLS-сертификата не совпал с тем, что показан на Mac."
        case .untrustedCertificate: "Сертификат Mac не прошёл проверку TLS."
        case .notPaired: "Сначала подтверди сопряжение на Mac."
        case .server(let message): message
        case .transport: "Mac недоступен по защищённой локальной сети. Команда не повторялась."
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
        // This one public certificate response is checked against a fingerprint the user
        // compares with the Mac UI. Every later request uses the pinned CA as a TLS anchor.
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
}

