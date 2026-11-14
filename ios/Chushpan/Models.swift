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
    var model = "gemini-3.8-flash"
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

struct MacRemoteDevice: Codable, Identifiable, Equatable {
    var id: String
    var name: String
    var kind: String
}

struct MacRemoteStatus: Codable, Equatable {
    var status: String
    var name: String?
    var capabilities: [String] = []
    var categories: [String] = []
    var scenarios: [MacRemoteScenario] = []
    var devices: [MacRemoteDevice] = []
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

        enum CodingKeys: String, CodingKey {
            case memory
            case mediaProgress = "media_progress"
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

struct MobileDatabase: Codable {
    static let currentSchema = 2

    var schemaVersion = currentSchema
    var deviceID = UUID().uuidString.lowercased()
    var conversations: [MobileConversation] = []
    var facts: [MobileMemoryFact] = []
    var media: [MobileMediaItem] = []
    var lightPresets: [MobileLightPreset] = []
    var sharedSettings = SharedSettings()
    var macMemoryCache: [MacMemoryCopy] = []
    var macMediaCache: [MacMediaProgress] = []
    var macCacheUpdatedAt: Date?

    enum CodingKeys: String, CodingKey {
        case schemaVersion, deviceID, conversations, facts, media, lightPresets
        case sharedSettings, macMemoryCache, macMediaCache, macCacheUpdatedAt
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
