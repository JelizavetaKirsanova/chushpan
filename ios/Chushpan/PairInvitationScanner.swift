import SwiftUI
import VisionKit

struct PairInvitationScanner: UIViewControllerRepresentable {
    let onInvite: (String) -> Void
    @Environment(\.dismiss) private var dismiss

    func makeCoordinator() -> Coordinator { Coordinator(onInvite: onInvite, dismiss: dismiss) }

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let controller = DataScannerViewController(
            recognizedDataTypes: [.barcode(symbologies: [.qr])],
            qualityLevel: .accurate,
            recognizesMultipleItems: false,
            isHighFrameRateTrackingEnabled: false,
            isHighlightingEnabled: true
        )
        controller.delegate = context.coordinator
        try? controller.startScanning()
        return controller
    }

    func updateUIViewController(_ uiViewController: DataScannerViewController, context: Context) {}

    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        private let onInvite: (String) -> Void
        private let dismiss: DismissAction

        init(onInvite: @escaping (String) -> Void, dismiss: DismissAction) {
            self.onInvite = onInvite
            self.dismiss = dismiss
        }

        func dataScanner(_ dataScanner: DataScannerViewController, didAdd addedItems: [RecognizedItem], allItems: [RecognizedItem]) {
            guard let url = addedItems.compactMap({ item -> String? in
                guard case .barcode(let barcode) = item else { return nil }
                return barcode.payloadStringValue
            }).first, url.hasPrefix("https://") else { return }
            onInvite(url)
            dataScanner.stopScanning()
            dismiss()
        }
    }
}
