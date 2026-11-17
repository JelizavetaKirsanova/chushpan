import Foundation

enum MobileStoreError: LocalizedError {
    case unsupportedSchema(Int)
    case invalidData

    var errorDescription: String? {
        switch self {
        case .unsupportedSchema(let version):
            "Версия локальной базы Чушпана не поддерживается: \(version). Исходный файл сохранён."
        case .invalidData:
            "Локальная база Чушпана повреждена. Исходный файл не перезаписан."
        }
    }
}

struct MobileStore {
    let fileURL: URL

    static func applicationDefault() throws -> MobileStore {
        let manager = FileManager.default
        let folder = try manager.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        ).appendingPathComponent("Chushpan", isDirectory: true)
        try manager.createDirectory(at: folder, withIntermediateDirectories: true)
        return MobileStore(fileURL: folder.appendingPathComponent("mobile-store.json"))
    }

    func load() throws -> MobileDatabase {
        guard FileManager.default.fileExists(atPath: fileURL.path) else { return MobileDatabase() }
        do {
            let raw = try Data(contentsOf: fileURL)
            let migrated = try Self.migrate(raw)
            return try JSONDecoder.mobile.decode(MobileDatabase.self, from: migrated)
        } catch let error as MobileStoreError {
            throw error
        } catch {
            throw MobileStoreError.invalidData
        }
    }

    func save(_ data: MobileDatabase) throws {
        var value = data
        value.schemaVersion = MobileDatabase.currentSchema
        let encoded = try JSONEncoder.mobile.encode(value)
        try FileManager.default.createDirectory(
            at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true
        )
        try encoded.write(to: fileURL, options: .atomic)
        #if os(iOS)
        try? FileManager.default.setAttributes(
            [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication],
            ofItemAtPath: fileURL.path
        )
        #endif
    }

    private static func migrate(_ raw: Data) throws -> Data {
        guard var root = try JSONSerialization.jsonObject(with: raw) as? [String: Any] else {
            throw MobileStoreError.invalidData
        }
        let version = root["schemaVersion"] as? Int ?? 1
        guard version <= MobileDatabase.currentSchema else {
            throw MobileStoreError.unsupportedSchema(version)
        }
        if version < 2 {
            root["deviceID"] = root["deviceID"] ?? UUID().uuidString.lowercased()
            root["lightPresets"] = root["lightPresets"] ?? []
            root["macMemoryCache"] = root["macMemoryCache"] ?? []
            root["macMediaCache"] = root["macMediaCache"] ?? []
            root["sharedSettings"] = root["sharedSettings"] ?? [
                "appearance": "system",
                "model": "gemini-3.8-flash",
                "updatedAt": ISO8601DateFormatter().string(from: Date()),
                "modifiedBy": "",
            ]
            root["schemaVersion"] = 2
        }
        return try JSONSerialization.data(withJSONObject: root, options: [.fragmentsAllowed, .sortedKeys])
    }
}

extension JSONEncoder {
    static var mobile: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }
}

extension JSONDecoder {
    static var mobile: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }
}
