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

    func testMacLibraryCacheKeepsLoadedEmptyStateAcrossRestart() throws {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("chushpan-library-cache-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: url) }
        var database = MobileDatabase()
        database.macLibraryCache = []
        database.macLibraryHasLoaded = true
        database.macCacheUpdatedAt = Date(timeIntervalSince1970: 100)
        try MobileStore(fileURL: url).save(database)

        let loaded = try MobileStore(fileURL: url).load()
        XCTAssertTrue(loaded.macLibraryHasLoaded)
        XCTAssertTrue(loaded.macLibraryCache.isEmpty)
        XCTAssertEqual(loaded.macCacheUpdatedAt, database.macCacheUpdatedAt)
    }

    func testMacLibraryPayloadMapsStatusFavoriteAndProgress() {
        let item = MacMediaItem(
            id: "title-1",
            title: "Фильм",
            kind: "series",
            year: 2024,
            posterURL: nil,
            description: nil,
            status: "watching",
            favorite: true,
            userRating: 8.5,
            watchlist: false,
            manualWatched: nil,
            addedAt: nil,
            libraryUpdatedAt: "2026-11-22T20:00:00.000+00:00",
            episodeID: "episode-1",
            season: 2,
            episode: 4,
            position: 120,
            duration: 6000,
            watched: false,
            lastViewed: nil
        )

        let mobile = item.mobileItem
        XCTAssertEqual(mobile.id, "title-1")
        XCTAssertEqual(mobile.kind, .series)
        XCTAssertEqual(mobile.status, .watching)
        XCTAssertTrue(mobile.favorite)
        XCTAssertEqual(mobile.rating, 8.5)
        XCTAssertEqual(mobile.season, 2)
        XCTAssertEqual(mobile.episode, 4)
        XCTAssertEqual(mobile.positionSeconds, 120)
    }

    func testCalendarPayloadWithoutCachedDaysStillDecodes() throws {
        let data = Data(#"{"today":"2026-11-22","status":"ready","message":"ok","timezone":"Europe/Tallinn","events":[]}"#.utf8)
        let snapshot = try JSONDecoder().decode(MacCalendarSnapshot.self, from: data)
        XCTAssertEqual(snapshot.today, "2026-11-22")
        XCTAssertNil(snapshot.days)
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

final class MacPairingCodeTests: XCTestCase {
    func testPairingCodeBindsChallengeDeviceAndCertificate() {
        let challenge = "test-challenge-12345678901234567890"
        let identity = String(repeating: "1", count: 64)
        let fingerprint = String(repeating: "ab", count: 32)
        XCTAssertEqual(
            MacBridgeClient.pairingCode(
                challenge: challenge,
                identity: identity,
                fingerprint: fingerprint
            ),
            "835016"
        )
        XCTAssertNotEqual(
            MacBridgeClient.pairingCode(
                challenge: challenge,
                identity: identity,
                fingerprint: String(repeating: "cd", count: 32)
            ),
            "835016"
        )
    }
}
