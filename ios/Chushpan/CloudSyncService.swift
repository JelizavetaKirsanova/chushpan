import CloudKit
import Foundation

enum CloudSyncError: LocalizedError {
    case accountUnavailable
    case malformedRecord

    var errorDescription: String? {
        switch self {
        case .accountUnavailable: "Войди в Apple Account на устройстве и проверь доступность iCloud."
        case .malformedRecord: "В iCloud найдена запись Чушпана неизвестного формата."
        }
    }
}

struct ConversationCloudMetadata: Codable {
    var id: String
    var title: String
    var isPrivate: Bool
    var createdAt: Date
    var updatedAt: Date
}

struct MessageCloudRecord: Codable {
    var conversationID: String
    var message: ChatMessage
}

final class CloudSyncService {
    private let container: CKContainer
    private let recordType = "MobileItem"

    init(container: CKContainer = CKContainer(identifier: "iCloud.ee.chushpan.mobile")) {
        self.container = container
    }

    func synchronize(local: [CloudItem]) async throws -> [CloudItem] {
        try Task.checkCancellation()
        guard try await container.accountStatus() == .available else {
            throw CloudSyncError.accountUnavailable
        }
        try Task.checkCancellation()
        let database = container.privateCloudDatabase
        let remote = try await fetchAll(from: database)
        var resolved: [String: CloudItem] = [:]
        for (id, localItem) in Dictionary(uniqueKeysWithValues: local.map { ($0.id, $0) }) {
            if let remoteItem = remote[id] {
                let winner = CloudConflictResolver.winner(local: localItem, remote: remoteItem)
                resolved[id] = winner
                if winner == localItem {
                    try Task.checkCancellation()
                    try await save(localItem, to: database)
                }
            } else {
                resolved[id] = localItem
                try Task.checkCancellation()
                try await save(localItem, to: database)
            }
        }
        for (id, remoteItem) in remote where resolved[id] == nil {
            resolved[id] = remoteItem
        }
        return Array(resolved.values)
    }

    private func fetchAll(from database: CKDatabase) async throws -> [String: CloudItem] {
        let query = CKQuery(recordType: recordType, predicate: NSPredicate(value: true))
        var result: [String: CloudItem] = [:]
        var (matches, cursor) = try await database.records(
            matching: query, desiredKeys: nil, resultsLimit: 100
        )
        while true {
            try Task.checkCancellation()
            for (recordID, recordResult) in matches {
                guard case .success(let record) = recordResult else {
                    if case .failure(let error) = recordResult { throw error }
                    throw CloudSyncError.malformedRecord
                }
                guard let item = Self.decode(record, fallbackID: recordID.recordName) else {
                    throw CloudSyncError.malformedRecord
                }
                result[item.id] = item
            }
            guard let next = cursor else { break }
            (matches, cursor) = try await database.records(
                continuingMatchFrom: next, desiredKeys: nil, resultsLimit: 100
            )
        }
        return result
    }

    private func save(_ item: CloudItem, to database: CKDatabase) async throws {
        let id = CKRecord.ID(recordName: item.id)
        let record = (try? await database.record(for: id))
            ?? CKRecord(recordType: recordType, recordID: id)
        record["kind"] = item.kind as CKRecordValue
        record["payload"] = item.payload as CKRecordValue
        record["updatedAt"] = item.updatedAt as CKRecordValue
        record["modifiedBy"] = item.modifiedBy as CKRecordValue
        record["deletedAt"] = item.deletedAt as CKRecordValue?
        _ = try await database.save(record)
    }

    private static func decode(_ record: CKRecord, fallbackID: String) -> CloudItem? {
        guard let kind = record["kind"] as? String,
              let payload = record["payload"] as? Data,
              let updatedAt = record["updatedAt"] as? Date,
              let modifiedBy = record["modifiedBy"] as? String else { return nil }
        return CloudItem(
            id: fallbackID,
            kind: kind,
            payload: payload,
            updatedAt: updatedAt,
            deletedAt: record["deletedAt"] as? Date,
            modifiedBy: modifiedBy
        )
    }
}

extension MobileDatabase {
    func cloudItems() throws -> [CloudItem] {
        let encoder = JSONEncoder.mobile
        var records: [CloudItem] = []
        for conversation in conversations {
            let meta = ConversationCloudMetadata(
                id: conversation.id,
                title: conversation.isPrivate ? "" : conversation.title,
                isPrivate: conversation.isPrivate,
                createdAt: conversation.createdAt,
                updatedAt: conversation.updatedAt
            )
            let hidden = conversation.isPrivate || conversation.deletedAt != nil
            records.append(CloudItem(
                id: conversation.id,
                kind: "conversation",
                payload: hidden ? Data() : try encoder.encode(meta),
                updatedAt: conversation.updatedAt,
                deletedAt: hidden ? (conversation.deletedAt ?? conversation.updatedAt) : nil,
                modifiedBy: conversation.modifiedBy.isEmpty ? deviceID : conversation.modifiedBy
            ))
            for message in conversation.messages {
                let payload = MessageCloudRecord(conversationID: conversation.id, message: message)
                records.append(CloudItem(
                    id: message.id,
                    kind: "message",
                    payload: hidden ? Data() : try encoder.encode(payload),
                    updatedAt: hidden
                        ? max(conversation.updatedAt, message.updatedAt ?? message.createdAt)
                        : (message.updatedAt ?? message.createdAt),
                    deletedAt: hidden ? (conversation.deletedAt ?? conversation.updatedAt) : nil,
                    modifiedBy: conversation.modifiedBy.isEmpty ? deviceID : conversation.modifiedBy
                ))
            }
        }
        for fact in facts {
            let hidden = fact.isPrivate || fact.deletedAt != nil
            records.append(CloudItem(
                id: fact.id,
                kind: "memory",
                payload: hidden ? Data() : try encoder.encode(fact),
                updatedAt: fact.updatedAt,
                deletedAt: hidden ? (fact.deletedAt ?? fact.updatedAt) : nil,
                modifiedBy: fact.modifiedBy.isEmpty ? deviceID : fact.modifiedBy
            ))
        }
        for item in media {
            records.append(CloudItem(
                id: item.id,
                kind: "media",
                payload: item.deletedAt == nil ? try encoder.encode(item) : Data(),
                updatedAt: item.updatedAt,
                deletedAt: item.deletedAt,
                modifiedBy: item.modifiedBy.isEmpty ? deviceID : item.modifiedBy
            ))
        }
        for preset in lightPresets {
            records.append(CloudItem(
                id: preset.id,
                kind: "lightPreset",
                payload: preset.deletedAt == nil ? try encoder.encode(preset) : Data(),
                updatedAt: preset.updatedAt,
                deletedAt: preset.deletedAt,
                modifiedBy: preset.modifiedBy.isEmpty ? deviceID : preset.modifiedBy
            ))
        }
        records.append(CloudItem(
            id: "shared-settings",
            kind: "settings",
            payload: try encoder.encode(sharedSettings),
            updatedAt: sharedSettings.updatedAt,
            modifiedBy: sharedSettings.modifiedBy.isEmpty ? deviceID : sharedSettings.modifiedBy
        ))
        return records
    }

    mutating func applyCloudItems(_ items: [CloudItem]) throws {
        let decoder = JSONDecoder.mobile
        for item in items.sorted(by: { $0.kind == "conversation" && $1.kind != "conversation" }) {
            switch item.kind {
            case "conversation":
                if let index = conversations.firstIndex(where: { $0.id == item.id }),
                   conversations[index].isPrivate, item.deletedAt != nil { continue }
                if let index = conversations.firstIndex(where: { $0.id == item.id }) {
                    conversations[index].title = (try? decoder.decode(ConversationCloudMetadata.self, from: item.payload).title)
                        ?? conversations[index].title
                    conversations[index].updatedAt = max(conversations[index].updatedAt, item.updatedAt)
                    conversations[index].deletedAt = item.deletedAt
                    conversations[index].modifiedBy = item.modifiedBy
                } else if item.deletedAt == nil,
                          let meta = try? decoder.decode(ConversationCloudMetadata.self, from: item.payload) {
                    conversations.append(MobileConversation(
                        id: meta.id,
                        title: meta.title,
                        createdAt: meta.createdAt,
                        updatedAt: meta.updatedAt,
                        modifiedBy: item.modifiedBy
                    ))
                }
            case "message":
                guard item.deletedAt == nil,
                      let value = try? decoder.decode(MessageCloudRecord.self, from: item.payload),
                      let index = conversations.firstIndex(where: { $0.id == value.conversationID && $0.deletedAt == nil }) else { continue }
                if let existing = conversations[index].messages.firstIndex(where: { $0.id == value.message.id }) {
                    let current = conversations[index].messages[existing]
                    if (value.message.updatedAt ?? value.message.createdAt)
                        >= (current.updatedAt ?? current.createdAt) {
                        conversations[index].messages[existing] = value.message
                    }
                } else {
                    conversations[index].messages.append(value.message)
                    conversations[index].messages.sort { $0.createdAt < $1.createdAt }
                }
            case "memory":
                if let index = facts.firstIndex(where: { $0.id == item.id }),
                   facts[index].isPrivate, item.deletedAt != nil { continue }
                if item.deletedAt != nil {
                    if let index = facts.firstIndex(where: { $0.id == item.id }) { facts[index].deletedAt = item.deletedAt }
                } else if let fact = try? decoder.decode(MobileMemoryFact.self, from: item.payload) {
                    replaceOrAppend(fact, in: &facts, id: item.id)
                }
            case "media":
                if item.deletedAt != nil {
                    if let index = media.firstIndex(where: { $0.id == item.id }) { media[index].deletedAt = item.deletedAt }
                } else if let value = try? decoder.decode(MobileMediaItem.self, from: item.payload) {
                    replaceOrAppend(value, in: &media, id: item.id)
                }
            case "lightPreset":
                if item.deletedAt != nil {
                    if let index = lightPresets.firstIndex(where: { $0.id == item.id }) { lightPresets[index].deletedAt = item.deletedAt }
                } else if let value = try? decoder.decode(MobileLightPreset.self, from: item.payload) {
                    replaceOrAppend(value, in: &lightPresets, id: item.id)
                }
            case "settings":
                if item.updatedAt >= sharedSettings.updatedAt,
                   let value = try? decoder.decode(SharedSettings.self, from: item.payload) {
                    sharedSettings = value
                }
            default:
                continue
            }
        }
    }

    private mutating func replaceOrAppend<T: Identifiable & Codable>(
        _ value: T, in list: inout [T], id: String
    ) where T.ID == String {
        if let index = list.firstIndex(where: { $0.id == id }) { list[index] = value }
        else { list.append(value) }
    }
}
