import Foundation
import XCTest
@testable import Chushpan

final class MobileStoreTests: XCTestCase {
    func testHomeFavoriteIDsRoundTripAndNormalizeDuplicates() {
        let encoded = MobileHomeFavorites.encode(["device-b", "scenario-a"])
        XCTAssertEqual(MobileHomeFavorites.decode(encoded), ["device-b", "scenario-a"])
        XCTAssertEqual(MobileHomeFavorites.decode(#"["scenario-a","scenario-a","device-b"]"#), ["scenario-a", "device-b"])
    }

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

    func testStoreWriterDoesNotReplaceNewerSnapshotWithOlderSave() async throws {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("chushpan-store-order-\(UUID().uuidString).json")
        defer { try? FileManager.default.removeItem(at: url) }
        let store = MobileStore(fileURL: url)
        let writer = MobileStoreWriter(store: store)
        var newer = MobileDatabase()
        newer.sharedSettings.appearance = "dark"
        var older = MobileDatabase()
        older.sharedSettings.appearance = "light"

        try await writer.save(newer, revision: 2)
        try await writer.save(older, revision: 1)

        XCTAssertEqual(try store.load().sharedSettings.appearance, "dark")
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
            posterURL: "https://image.tmdb.org/t/p/w500/poster.jpg",
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
        XCTAssertEqual(mobile.posterURL, "https://image.tmdb.org/t/p/w500/poster.jpg")
    }

    func testRemoteDevicePayloadPreservesOfflineStateAndLocation() throws {
        let data = Data(#"{"id":"device-a","name":"Лента","kind":"led_strip","protocol":"ble","endpoint":"device-uuid","place_id":"home","place_name":"Дом","room":"Кухня","state":{"brightness":42,"muted":false},"sent":{"brightness":55},"favorites":["warm"],"favorite":true,"ble_name":"ELK-BLEDDM 39","connection":"disconnected","usable":true,"availability":"offline"}"#.utf8)

        let device = try JSONDecoder.mobile.decode(MacRemoteDevice.self, from: data)

        XCTAssertEqual(device.id, "device-a")
        XCTAssertEqual(device.kind, "led_strip")
        XCTAssertEqual(device.protocolName, "ble")
        XCTAssertEqual(device.endpoint, "device-uuid")
        XCTAssertEqual(device.locationText, "Кухня · Дом")
        XCTAssertEqual(device.brightness, 42)
        XCTAssertEqual(device.isMuted, false)
        XCTAssertEqual(device.sent?["brightness"]?.intValue, 55)
        XCTAssertEqual(device.favorites, ["warm"])
        XCTAssertEqual(device.bleName, "ELK-BLEDDM 39")
        XCTAssertEqual(device.availabilityText, "Оффлайн · команды не отправляются")
    }

    func testOlderMobileMediaPayloadDecodesWithoutPosterURL() throws {
        let data = Data(#"{"id":"local","title":"Фильм","kind":"Фильм","status":"Хочу посмотреть","favorite":false,"season":0,"episode":0,"positionSeconds":0,"durationSeconds":0,"webURL":"","updatedAt":"2026-11-22T20:00:00Z","modifiedBy":""}"#.utf8)

        let item = try JSONDecoder.mobile.decode(MobileMediaItem.self, from: data)

        XCTAssertEqual(item.id, "local")
        XCTAssertNil(item.posterURL)
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

final class MobileHomeLayoutTests: XCTestCase {
    func testHomeLayoutRoundTripPreservesCustomizationOrder() {
        let widgets: [MobileHomeWidget] = [.quickActions, .voice, .chat]
        XCTAssertEqual(MobileHomeWidget.decode(MobileHomeWidget.encode(widgets)), widgets)

        let actions: [MobileHomeAction] = [.memory, .lighting, .calendar]
        XCTAssertEqual(MobileHomeAction.decode(MobileHomeAction.encode(actions)), actions)
    }

    func testHomeLayoutDropsUnknownAndDuplicateItems() {
        XCTAssertEqual(
            MobileHomeWidget.decode("[\"chat\",\"unknown\",\"voice\",\"chat\"]"),
            [.chat, .voice]
        )
        XCTAssertEqual(
            MobileHomeAction.decode("[\"calendar\",\"unknown\",\"memory\",\"calendar\"]"),
            [.calendar, .memory]
        )
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
