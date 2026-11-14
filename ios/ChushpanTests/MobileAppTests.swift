import Foundation
import XCTest
@testable import Chushpan

final class MobileStoreTests: XCTestCase {
    func testStoreRoundTripKeepsStableIDsAndPrivateMode() throws {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("chushpan-store-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: url) }
        let store = MobileStore(fileURL: url)
        var database = MobileDatabase()
        let conversation = MobileConversation(title: "Тест", messages: [
            ChatMessage(role: .user, text: "Проверка"),
        ], isPrivate: true)
        database.conversations = [conversation]
        try store.save(database)

        let loaded = try store.load()
        XCTAssertEqual(loaded.conversations.first?.id, conversation.id)
        XCTAssertEqual(loaded.conversations.first?.messages.first?.text, "Проверка")
        XCTAssertTrue(loaded.conversations.first?.isPrivate == true)
    }

    func testVersionOneStoreMigratesWithoutImportingMacData() throws {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("chushpan-migration-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: url) }
        let legacy = Data(#"{"schemaVersion":1,"conversations":[],"facts":[],"media":[]}"#.utf8)
        try legacy.write(to: url)

        let migrated = try MobileStore(fileURL: url).load()
        XCTAssertEqual(migrated.schemaVersion, MobileDatabase.currentSchema)
        XCTAssertFalse(migrated.deviceID.isEmpty)
        XCTAssertTrue(migrated.macMemoryCache.isEmpty)
        XCTAssertTrue(migrated.macMediaCache.isEmpty)
    }

    func testPrivateContentIsNotIncludedInCloudPayload() throws {
        var database = MobileDatabase()
        database.conversations = [
            MobileConversation(title: "Частный разговор", messages: [
                ChatMessage(role: .user, text: "не отправлять"),
            ], isPrivate: true),
            MobileConversation(title: "Обычный разговор", messages: [
                ChatMessage(role: .user, text: "обычный текст"),
            ]),
        ]
        database.facts = [
            MobileMemoryFact(content: "частный факт", isPrivate: true),
            MobileMemoryFact(content: "обычный факт"),
        ]

        let records = try database.cloudItems()
        let privateConversation = try XCTUnwrap(records.first { $0.kind == "conversation" && $0.payload.isEmpty })
        let privateMessage = try XCTUnwrap(records.first { $0.kind == "message" && $0.payload.isEmpty })
        let privateFact = try XCTUnwrap(records.first { $0.kind == "memory" && $0.payload.isEmpty })
        XCTAssertNotNil(privateConversation.deletedAt)
        XCTAssertNotNil(privateMessage.deletedAt)
        XCTAssertNotNil(privateFact.deletedAt)
        XCTAssertFalse(records.compactMap { String(data: $0.payload, encoding: .utf8) }
            .joined().contains("частный"))
    }

    func testNewerTombstoneWinsAndEqualStampIsDeterministic() {
        let date = Date(timeIntervalSince1970: 100)
        let live = CloudItem(id: "same", kind: "memory", payload: Data("live".utf8), updatedAt: date, modifiedBy: "device-a")
        let tombstone = CloudItem(id: "same", kind: "memory", payload: Data(), updatedAt: date.addingTimeInterval(1), deletedAt: date.addingTimeInterval(1), modifiedBy: "device-b")
        XCTAssertEqual(CloudConflictResolver.winner(local: live, remote: tombstone), tombstone)

        let tie = CloudItem(id: "same", kind: "memory", payload: Data("remote".utf8), updatedAt: date, modifiedBy: "device-b")
        XCTAssertEqual(CloudConflictResolver.winner(local: live, remote: tie), tie)
        XCTAssertEqual(CloudConflictResolver.winner(local: tie, remote: live), tie)
    }

    func testTombstoneWinsEqualTimestampToPreventDeletedContentReturning() {
        let date = Date(timeIntervalSince1970: 100)
        let live = CloudItem(id: "same", kind: "memory", payload: Data("secret".utf8), updatedAt: date, modifiedBy: "device-z")
        let tombstone = CloudItem(id: "same", kind: "memory", payload: Data(), updatedAt: date, deletedAt: date, modifiedBy: "device-a")
        XCTAssertEqual(CloudConflictResolver.winner(local: live, remote: tombstone), tombstone)
        XCTAssertEqual(CloudConflictResolver.winner(local: tombstone, remote: live), tombstone)
    }
}

final class GeminiClientTests: XCTestCase {
    func testHTTPErrorsAreMappedWithoutMakingALiveRequest() {
        XCTAssertEqual(GeminiError.forHTTPStatus(401), .unauthorized)
        XCTAssertEqual(GeminiError.forHTTPStatus(403), .forbidden)
        XCTAssertEqual(GeminiError.forHTTPStatus(429), .rateLimited)
        XCTAssertEqual(GeminiError.forHTTPStatus(404), .modelUnavailable)
        XCTAssertEqual(GeminiError.forHTTPStatus(503), .serverUnavailable)
    }

    func testSSETextDeltaParserIgnoresNonTextEvents() {
        let data = Data(#"{"candidates":[{"content":{"parts":[{"text":"Привет"}]}}]}"#.utf8)
        XCTAssertEqual(GeminiClient.textDelta(from: data), "Привет")
        XCTAssertNil(GeminiClient.textDelta(from: Data("not json".utf8)))
        let blocked = Data(#"{"candidates":[{"content":{"parts":[{}]}}]}"#.utf8)
        XCTAssertEqual(GeminiClient.textDelta(from: blocked), "")
    }

    @MainActor
    func testCancellingStreamCancelsUnderlyingURLRequest() async throws {
        let started = expectation(description: "stream request started")
        HeldResponseURLProtocol.onStart = { started.fulfill() }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [HeldResponseURLProtocol.self]
        let session = URLSession(configuration: configuration)
        let client = GeminiClient(session: session, baseURL: URL(string: "https://mock.chushpan.invalid")!)
        let task = Task { @MainActor in
            try await client.stream(
                key: "test-key-never-sent-to-network",
                model: "gemini-test",
                messages: [GeminiInputMessage(role: .user, text: "Привет")],
                memoryContext: "",
                onText: { _ in }
            )
        }
        await fulfillment(of: [started], timeout: 2)
        task.cancel()
        do {
            try await task.value
            XCTFail("Ожидалась отмена запроса")
        } catch is CancellationError {
            XCTAssertTrue(HeldResponseURLProtocol.wasStopped)
        }
        session.invalidateAndCancel()
    }
}

private final class HeldResponseURLProtocol: URLProtocol {
    static var onStart: (() -> Void)?
    static var wasStopped = false
    private var stopped = false

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.wasStopped = false
        Self.onStart?()
        guard let response = HTTPURLResponse(
            url: request.url!, statusCode: 200, httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "text/event-stream"]
        ) else { return }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        DispatchQueue.global().asyncAfter(deadline: .now() + 2) { [weak self] in
            guard let self, !self.stopped else { return }
            self.client?.urlProtocol(self, didLoad: Data("data: {\"candidates\":[]}\n\n".utf8))
            self.client?.urlProtocolDidFinishLoading(self)
        }
    }

    override func stopLoading() {
        stopped = true
        Self.wasStopped = true
    }
}
