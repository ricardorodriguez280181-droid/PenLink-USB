import SwiftUI

/// Bridges the UIKit PenView into SwiftUI and wires it to the server.
struct PenSurface: UIViewRepresentable {
    let server: USBServer
    let onToggleLog: () -> Void

    func makeUIView(context: Context) -> PenView {
        let view = PenView()
        view.onSamples = { [weak server] samples in
            server?.sendPen(samples)
        }
        view.onToggleLog = onToggleLog
        server.onFrame = { [weak view] image in
            view?.show(image)
        }
        return view
    }

    func updateUIView(_ uiView: PenView, context: Context) {
        uiView.onToggleLog = onToggleLog
    }
}

struct ContentView: View {
    @StateObject private var server = USBServer()
    @ObservedObject private var log = AppLog.shared
    @Environment(\.scenePhase) private var scenePhase
    @State private var showLog = false

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            PenSurface(server: server, onToggleLog: { showLog.toggle() })
                .ignoresSafeArea()

            if !server.connected {
                VStack(spacing: 10) {
                    Text("PenLink")
                        .font(.largeTitle.bold())
                    Text(server.status)
                        .font(.title3)
                    Text("Connect the USB cable and start PenLink-USB on the PC.\nThree-finger tap shows the log.")
                        .font(.footnote)
                        .multilineTextAlignment(.center)
                        .opacity(0.6)
                }
                .foregroundColor(.white)
                .padding()
                .allowsHitTesting(false)
            }

            if showLog {
                VStack(alignment: .leading, spacing: 2) {
                    Text(server.status).bold()
                    ForEach(Array(log.lines.suffix(14).enumerated()), id: \.offset) { item in
                        Text(item.element)
                    }
                }
                .font(.system(size: 12, design: .monospaced))
                .foregroundColor(.green)
                .padding(8)
                .background(Color.black.opacity(0.7))
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                .allowsHitTesting(false)
            }
        }
        .statusBarHidden(true)
        .onAppear {
            UIApplication.shared.isIdleTimerDisabled = true   // no Auto-Lock while the app is open
            server.start()
        }
        .onChange(of: scenePhase) { phase in
            // iOS suspends network listeners in the background, so restart when we come back.
            if phase == .active {
                server.start()
            } else if phase == .background {
                server.stop()
            }
        }
    }
}
