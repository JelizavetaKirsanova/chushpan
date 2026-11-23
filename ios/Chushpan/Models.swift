import Foundation

enum ChatRole: String, Codable {
    case user
    case assistant
}

enum ChatDelivery: String, Codable {
    case complete
    case pending
    case streaming
    case failed
    case cancelled
}

struct ChatMessage: Codable, Identifiable, Equatable {
    var id: String = UUID().uuidString.lowercased()
    var role: ChatRole
    var text: String
    var createdAt: Date = .now
    var updatedAt: Date?
    var delivery: ChatDelivery = .complete
    var errorMessage: String?
    var replyToMessageID: String?
    var provider: String?
    var model: String?
}

struct MobileConversation: Codable, Identifiable, Equatable {
    var id: String = UUID().uuidString.lowercased()
    var title: String = "Новый диалог"
    var messages: [ChatMessage] = []
    var isPrivate = false
    var createdAt: Date = .now
    var updatedAt: Date = .now
    var deletedAt: Date?
    var modifiedBy = ""
}

struct MobileMemoryFact: Codable, Identifiable, Equatable {
    var id: String = UUID().uuidString.lowercased()
    var content: String
    var category: String = "Общее"
    var isPrivate = false
    var createdAt: Date = .now
    var updatedAt: Date = .now
    var deletedAt: Date?
    var modifiedBy = ""
}

enum MediaKind: String, Codable, CaseIterable, Identifiable {
    case film = "Фильм"
    case series = "Сериал"
    var id: String { rawValue }
}

enum MediaStatus: String, Codable, CaseIterable, Identifiable {
    case planned = "Хочу посмотреть"
    case watching = "Смотрю"
    case watched = "Просмотрено"
    var id: String { rawValue }
}

struct MobileMediaItem: Codable, Identifiable, Equatable {
    var id: String = UUID().uuidString.lowercased()
    var title: String
    var kind: MediaKind = .film
    var year: Int?
    var status: MediaStatus = .planned
    var favorite = false
    var rating: Double?
    var season = 0
    var episode = 0
    var positionSeconds: Double = 0
    var durationSeconds: Double = 0
    var webURL: String = ""
    var updatedAt: Date = .now
    var deletedAt: Date?
    var modifiedBy = ""
    var posterURL: String?
}

struct MacMediaItem: Codable, Identifiable, Equatable {
    var id: String
    var title: String
    var kind: String
    var year: Int?
    var posterURL: String?
    var description: String?
    var status: String
    var favorite: Bool
    var userRating: Double?
    var watchlist: Bool
    var manualWatched: Bool?
    var addedAt: String?
    var libraryUpdatedAt: String?
    var episodeID: String?
    var season: Int?
    var episode: Int?
    var position: Double?
    var duration: Double?
    var watched: Bool?
    var lastViewed: String?

    enum CodingKeys: String, CodingKey {
        case id, title, kind, year, description, status, favorite, watchlist, season, episode
        case posterURL = "poster_url"
        case userRating = "user_rating"
        case manualWatched = "manual_watched"
        case addedAt = "added_at"
        case libraryUpdatedAt = "library_updated_at"
        case episodeID = "episode_id"
        case position, duration, watched
        case lastViewed = "last_viewed"
    }

    var mobileItem: MobileMediaItem {
        let timestamp = libraryUpdatedAt.flatMap { value -> Date? in
            let fractional = ISO8601DateFormatter()
            fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            return fractional.date(from: value) ?? ISO8601DateFormatter().date(from: value)
        } ?? .distantPast
        let libraryStatus: MediaStatus
        switch status {
        case "watching": libraryStatus = .watching
        case "watched": libraryStatus = .watched
        default: libraryStatus = .planned
        }
        return MobileMediaItem(
            id: id,
            title: title,
            kind: kind == "series" ? .series : .film,
            year: year,
            status: libraryStatus,
            favorite: favorite,
            rating: userRating,
            season: season ?? 0,
            episode: episode ?? 0,
            positionSeconds: position ?? 0,
            durationSeconds: duration ?? 0,
            webURL: "",
            updatedAt: timestamp,
            posterURL: posterURL
        )
    }
}

struct RGBColor: Codable, Equatable {
    var red = 145
    var green = 96
    var blue = 210

    var hex: String { String(format: "#%02X%02X%02X", red, green, blue) }
}

struct MobileLightPreset: Codable, Identifiable, Equatable {
    var id: String = UUID().uuidString.lowercased()
    var name: String
    var rgb = RGBColor()
    var brightness = 60
    var isFavorite = false
    var updatedAt: Date = .now
    var deletedAt: Date?
    var modifiedBy = ""
}

struct SharedSettings: Codable, Equatable {
    var appearance = "system"
    var model = "gpt-4.1-mini"
    var updatedAt: Date = .now
    var modifiedBy = ""
}

struct MacRemoteScenario: Codable, Identifiable, Equatable {
    var id: String
    var name: String
    var revision: Int
    var variants: [MacScenarioVariant] = []
    var variables: [MacScenarioVariable] = []

    enum CodingKeys: String, CodingKey { case id, name, revision, variants, variables }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        name = try values.decode(String.self, forKey: .name)
        revision = try values.decode(Int.self, forKey: .revision)
        variants = try values.decodeIfPresent([MacScenarioVariant].self, forKey: .variants) ?? []
        variables = try values.decodeIfPresent([MacScenarioVariable].self, forKey: .variables) ?? []
    }
}

struct MacScenarioVariant: Codable, Identifiable, Equatable {
    var id: String
    var name: String
    var placeID: String
    var enabled: Bool

    enum CodingKeys: String, CodingKey {
        case id, name, enabled
        case placeID = "place_id"
    }
}

struct MacScenarioVariable: Codable, Identifiable, Equatable {
    var name: String
    var kind: String
    var id: String { name }
}

indirect enum MobileJSONValue: Codable, Equatable {
    case null
    case bool(Bool)
    case integer(Int)
    case number(Double)
    case string(String)
    case array([MobileJSONValue])
    case object([String: MobileJSONValue])

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let decoded = try? value.decode(Bool.self) { self = .bool(decoded) }
        else if let decoded = try? value.decode(Int.self) { self = .integer(decoded) }
        else if let decoded = try? value.decode(Double.self) { self = .number(decoded) }
        else if let decoded = try? value.decode(String.self) { self = .string(decoded) }
        else if let decoded = try? value.decode([MobileJSONValue].self) { self = .array(decoded) }
        else if let decoded = try? value.decode([String: MobileJSONValue].self) { self = .object(decoded) }
        else {
            throw DecodingError.dataCorruptedError(in: value, debugDescription: "Unsupported JSON value")
        }
    }

    func encode(to encoder: Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case .null: try value.encodeNil()
        case .bool(let decoded): try value.encode(decoded)
        case .integer(let decoded): try value.encode(decoded)
        case .number(let decoded): try value.encode(decoded)
        case .string(let decoded): try value.encode(decoded)
        case .array(let decoded): try value.encode(decoded)
        case .object(let decoded): try value.encode(decoded)
        }
    }

    var intValue: Int? {
        switch self {
        case .integer(let value): value
        case .number(let value): Int(exactly: value)
        default: nil
        }
    }

    var stringValue: String? {
        guard case .string(let value) = self else { return nil }
        return value
    }
}

struct MacRemoteDevice: Codable, Identifiable, Equatable {
    var id: String
    var revision: Int?
    var name: String
    var kind: String
    var protocolName: String?
    var endpoint: String?
    var placeID: String?
    var placeName: String?
    var room: String?
    var state: [String: MobileJSONValue]?
    var sent: [String: MobileJSONValue]?
    var lastContact: String?
    var stateAt: String?
    var resultStatus: String?
    var favorites: [String]?
    var aliases: [String]?
    var favorite: Bool?
    var autoConnect: Bool?
    var bleName: String?
    var features: [String: Bool]?
    var ble: [String: MobileJSONValue]?
    var connection: String?
    var connectionError: String?
    var controlReason: String?
    var usable: Bool?
    var reason: String?
    var busy: Bool?
    var availability: String?

    enum CodingKeys: String, CodingKey {
        case id, revision, name, kind, endpoint, room, state, sent, favorites, aliases
        case favorite, features, ble, usable, reason, busy, availability, connection
        case protocolName = "protocol"
        case placeID = "place_id"
        case placeName = "place_name"
        case lastContact = "last_contact"
        case stateAt = "state_at"
        case resultStatus = "result_status"
        case autoConnect = "auto_connect"
        case bleName = "ble_name"
        case connectionError = "connection_error"
        case controlReason = "control_reason"
    }

    var brightness: Int? { state?["brightness"]?.intValue }
    var volumePercent: Int? { state?["volume_percent"]?.intValue }
    var isMuted: Bool? {
        guard case .bool(let value) = state?["muted"] else { return nil }
        return value
    }

    var locationText: String? {
        [room, placeName ?? placeID].compactMap { value in
            let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed?.isEmpty == false ? trimmed : nil
        }.joined(separator: " · ").nilIfEmpty
    }

    var availabilityText: String {
        switch availability ?? "unknown" {
        case "online": "На связи"
        case "offline": "Оффлайн · команды не отправляются"
        case "remote-unavailable": reason ?? connectionError ?? "Сейчас недоступно через Mac"
        default: connection == "observed" ? "Подключение проверено" : "Подключение не проверено"
        }
    }
}

private extension String {
    var nilIfEmpty: String? { isEmpty ? nil : self }
}

struct MacRemoteStatus: Codable, Equatable {
    var status: String
    var name: String?
    var capabilities: [String] = []
    var categories: [String] = []
    var scenarios: [MacRemoteScenario] = []
    var devices: [MacRemoteDevice] = []
    var mobileChat: MacChatProviderStatus?

    enum CodingKeys: String, CodingKey {
        case status, name, capabilities, categories, scenarios, devices
        case mobileChat = "mobile_chat"
    }
}

struct MacChatProviderStatus: Codable, Equatable {
    var provider: String
    var model: String
    var fallback: Bool
    var desktopProvider: String?

    enum CodingKeys: String, CodingKey {
        case provider, model, fallback
        case desktopProvider = "desktop_provider"
    }
}

struct MacMemoryCopy: Codable, Identifiable, Equatable {
    var id: Int
    var content: String
    var category: String
    var revision: Int
    var updatedAt: String

    enum CodingKeys: String, CodingKey {
        case id, content, category, revision
        case updatedAt = "updated_at"
    }
}

struct MacSyncResponse: Codable {
    struct Payload: Codable {
        var memory: [MacMemoryCopy]?
        var mediaProgress: [MacMediaProgress]?
        var mediaLibrary: [MacMediaItem]?
        var history: [MacHistoryCopy]?
        var calendar: MacCalendarSnapshot?
        var statistics: MacStatisticsSnapshot?
        var mobileChat: MacChatProviderStatus?

        enum CodingKeys: String, CodingKey {
            case memory, history, calendar, statistics
            case mediaProgress = "media_progress"
            case mediaLibrary = "media_library"
            case mobileChat = "mobile_chat"
        }
    }
    var schema: Int
    var revision: String
    var data: Payload
    var serverTime: Double

    enum CodingKeys: String, CodingKey {
        case schema, revision, data
        case serverTime = "server_time"
    }
}

struct MacHistoryCopy: Codable, Identifiable, Equatable {
    var id: Int
    var role: ChatRole
    var content: String
    var createdAt: String

    enum CodingKeys: String, CodingKey {
        case id, role, content
        case createdAt = "created_at"
    }
}

struct MacCalendarEvent: Codable, Identifiable, Equatable {
    var id: String
    var title: String
    var color: String?
    var start: String
    var end: String
    var allDay: Bool
    var calendar: String?

    enum CodingKeys: String, CodingKey {
        case id, title, color, start, end, calendar
        case allDay = "all_day"
    }
}

struct MacCalendarSnapshot: Codable, Equatable {
    var today: String
    var status: String
    var message: String
    var timezone: String
    var updatedAt: String?
    var days: [MacCalendarDay]?
    var events: [MacCalendarEvent]

    enum CodingKeys: String, CodingKey {
        case today, status, message, timezone, events, days
        case updatedAt = "updated_at"
    }
}

struct MacCalendarDay: Codable, Identifiable, Equatable {
    var date: String
    var status: String
    var message: String
    var timezone: String
    var updatedAt: String?
    var events: [MacCalendarEvent]
    var id: String { date }

    enum CodingKeys: String, CodingKey {
        case date, status, message, timezone, events
        case updatedAt = "updated_at"
    }
}

struct MacStatisticsSnapshot: Codable, Equatable {
    struct Stage: Codable, Equatable {
        var stt: Double?
        var modelTotal: Double?
        var ttsGeneration: Double?

        enum CodingKeys: String, CodingKey {
            case stt
            case modelTotal = "model_total"
            case ttsGeneration = "tts_generation"
        }
    }

    struct Bucket: Codable, Identifiable, Equatable {
        var label: String
        var requests: Int
        var response: Double?
        var id: String { label }

        enum CodingKeys: String, CodingKey {
            case label, requests
            case response = "response_s"
        }
    }

    var days: Int
    var conversations: Int
    var messages: Int
    var response: Double?
    var stages: Stage
    var commands: Int
    var errors: Int
    var model: String?
    var buckets: [Bucket]

    enum CodingKeys: String, CodingKey {
        case days, conversations, messages, stages, commands, errors, model, buckets
        case response = "response_s"
    }
}

struct MacMediaProgress: Codable, Identifiable, Equatable {
    var id: String
    var title: String
    var season: Int
    var episode: Int
    var position: Double
    var duration: Double
    var watched: Bool
    var lastViewed: String?

    enum CodingKeys: String, CodingKey {
        case id, title, season, episode, position, duration, watched
        case lastViewed = "last_viewed"
    }
}

struct MobileDatabase: Codable, @unchecked Sendable {
    static let currentSchema = 3

    var schemaVersion = currentSchema
    var deviceID = UUID().uuidString.lowercased()
    var conversations: [MobileConversation] = []
    var facts: [MobileMemoryFact] = []
    var media: [MobileMediaItem] = []
    var lightPresets: [MobileLightPreset] = []
    var sharedSettings = SharedSettings()
    var macMemoryCache: [MacMemoryCopy] = []
    var macMediaCache: [MacMediaProgress] = []
    var macLibraryCache: [MacMediaItem] = []
    var macLibraryHasLoaded = false
    var macHistoryCache: [MacHistoryCopy] = []
    var macCalendarCache: MacCalendarSnapshot?
    var macStatisticsCache: MacStatisticsSnapshot?
    var macCacheUpdatedAt: Date?

    enum CodingKeys: String, CodingKey {
        case schemaVersion, deviceID, conversations, facts, media, lightPresets
        case sharedSettings, macMemoryCache, macMediaCache, macCacheUpdatedAt
        case macLibraryCache, macLibraryHasLoaded, macHistoryCache, macCalendarCache, macStatisticsCache
    }

    init() {}

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decodeIfPresent(Int.self, forKey: .schemaVersion) ?? 1
        deviceID = try values.decodeIfPresent(String.self, forKey: .deviceID)
            ?? UUID().uuidString.lowercased()
        conversations = try values.decodeIfPresent([MobileConversation].self, forKey: .conversations) ?? []
        facts = try values.decodeIfPresent([MobileMemoryFact].self, forKey: .facts) ?? []
        media = try values.decodeIfPresent([MobileMediaItem].self, forKey: .media) ?? []
        lightPresets = try values.decodeIfPresent([MobileLightPreset].self, forKey: .lightPresets) ?? []
        sharedSettings = try values.decodeIfPresent(SharedSettings.self, forKey: .sharedSettings)
            ?? SharedSettings()
        macMemoryCache = try values.decodeIfPresent([MacMemoryCopy].self, forKey: .macMemoryCache) ?? []
        macMediaCache = try values.decodeIfPresent([MacMediaProgress].self, forKey: .macMediaCache) ?? []
        macLibraryCache = try values.decodeIfPresent([MacMediaItem].self, forKey: .macLibraryCache) ?? []
        macLibraryHasLoaded = try values.decodeIfPresent(Bool.self, forKey: .macLibraryHasLoaded) ?? false
        macHistoryCache = try values.decodeIfPresent([MacHistoryCopy].self, forKey: .macHistoryCache) ?? []
        macCalendarCache = try values.decodeIfPresent(MacCalendarSnapshot.self, forKey: .macCalendarCache)
        macStatisticsCache = try values.decodeIfPresent(MacStatisticsSnapshot.self, forKey: .macStatisticsCache)
        macCacheUpdatedAt = try values.decodeIfPresent(Date.self, forKey: .macCacheUpdatedAt)
        schemaVersion = Self.currentSchema
    }
}

struct CloudItem: Codable, Equatable, Identifiable {
    var id: String
    var kind: String
    var payload: Data
    var updatedAt: Date
    var deletedAt: Date?
    var modifiedBy: String
}

enum CloudConflictResolver {
    static func winner(local: CloudItem, remote: CloudItem) -> CloudItem {
        if local.updatedAt != remote.updatedAt {
            return local.updatedAt > remote.updatedAt ? local : remote
        }
        if (local.deletedAt != nil) != (remote.deletedAt != nil) {
            return local.deletedAt != nil ? local : remote
        }
        if local.modifiedBy != remote.modifiedBy {
            return local.modifiedBy > remote.modifiedBy ? local : remote
        }
        // Equal stamps from the same device should be byte-stable across repeat syncs.
        return local.payload.lexicographicallyPrecedes(remote.payload) ? remote : local
    }
}
