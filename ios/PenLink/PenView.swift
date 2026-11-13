import UIKit

/// Shows the PC picture and turns Apple Pencil touches into pen samples.
/// Fingers and palms are ignored on purpose, which gives free palm rejection.
final class PenView: UIView {
    var onSamples: (([PenSample]) -> Void)?
    var onToggleLog: (() -> Void)?

    private let imageView = UIImageView()
    private var activeTouch: UITouch?

    override init(frame: CGRect) {
        super.init(frame: frame)
        setup()
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        setup()
    }

    private func setup() {
        backgroundColor = .black
        // Needed so the Pencil still works while a palm or finger rests on the screen.
        isMultipleTouchEnabled = true

        imageView.contentMode = .scaleAspectFit
        imageView.backgroundColor = .black
        imageView.isUserInteractionEnabled = false
        addSubview(imageView)

        // Pencil hover (iPad Pro M2 and newer). Other iPads simply never report it.
        let hover = UIHoverGestureRecognizer(target: self, action: #selector(handleHover(_:)))
        addGestureRecognizer(hover)

        // Three fingers tap = show or hide the log. Fingers do nothing else.
        let tap = UITapGestureRecognizer(target: self, action: #selector(handleLogTap))
        tap.numberOfTouchesRequired = 3
        tap.allowedTouchTypes = [NSNumber(value: UITouch.TouchType.direct.rawValue)]
        addGestureRecognizer(tap)
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        imageView.frame = bounds
    }

    func show(_ image: UIImage) {
        imageView.image = image
    }

    // MARK: geometry

    /// Where the picture really is inside the view (aspect fit leaves black bars).
    private func pictureRect() -> CGRect {
        guard let size = imageView.image?.size, size.width > 0, size.height > 0,
              bounds.width > 0, bounds.height > 0 else { return bounds }
        let scale = min(bounds.width / size.width, bounds.height / size.height)
        let w = size.width * scale
        let h = size.height * scale
        return CGRect(x: (bounds.width - w) / 2, y: (bounds.height - h) / 2, width: w, height: h)
    }

    private func normalized(_ p: CGPoint) -> (Double, Double) {
        let r = pictureRect()
        if r.width <= 0 || r.height <= 0 { return (0, 0) }
        let x = min(max(Double((p.x - r.minX) / r.width), 0), 1)
        let y = min(max(Double((p.y - r.minY) / r.height), 0), 1)
        return (x, y)
    }

    // MARK: Pencil touches

    private func sample(_ touch: UITouch, state: Int) -> PenSample {
        let (x, y) = normalized(touch.location(in: self))

        var pressure = 0.0
        if state == PenSample.contact {
            let maxForce = touch.maximumPossibleForce
            pressure = maxForce > 0 ? Double(touch.force / maxForce) : 0.5
            pressure = min(max(pressure, 0), 1)
        }

        // Same convention as the web Pointer Events tiltX / tiltY, in degrees.
        let altitude = Double(touch.altitudeAngle)
        let azimuth = Double(touch.azimuthAngle(in: self))
        let tanAltitude = tan(altitude)
        var tiltX = atan2(cos(azimuth), tanAltitude) * 180.0 / Double.pi
        var tiltY = atan2(sin(azimuth), tanAltitude) * 180.0 / Double.pi
        tiltX = min(max(tiltX.isFinite ? tiltX : 0, -90), 90)
        tiltY = min(max(tiltY.isFinite ? tiltY : 0, -90), 90)

        return PenSample(x: x, y: y, pressure: pressure,
                         tiltX: Int(tiltX.rounded()), tiltY: Int(tiltY.rounded()), state: state)
    }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        guard activeTouch == nil,
              let touch = touches.first(where: { $0.type == .pencil }) else { return }
        activeTouch = touch
        onSamples?([sample(touch, state: PenSample.contact)])
    }

    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent?) {
        guard let touch = activeTouch, touches.contains(touch) else { return }
        // Every intermediate position, at the Pencil's own rate.
        let all = event?.coalescedTouches(for: touch) ?? [touch]
        onSamples?(all.map { sample($0, state: PenSample.contact) })
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
        guard let touch = activeTouch, touches.contains(touch) else { return }
        activeTouch = nil
        // Lift, then leave range. If the iPad reports hover, the next hover sample brings the pen back.
        onSamples?([sample(touch, state: PenSample.hover), sample(touch, state: PenSample.out)])
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {
        guard let touch = activeTouch, touches.contains(touch) else { return }
        activeTouch = nil
        onSamples?([sample(touch, state: PenSample.out)])
    }

    // MARK: hover and log gesture

    @objc private func handleHover(_ gesture: UIHoverGestureRecognizer) {
        if activeTouch != nil { return }
        let (x, y) = normalized(gesture.location(in: self))
        switch gesture.state {
        case .began, .changed:
            onSamples?([PenSample(x: x, y: y, pressure: 0, tiltX: 0, tiltY: 0, state: PenSample.hover)])
        case .ended, .cancelled:
            onSamples?([PenSample(x: x, y: y, pressure: 0, tiltX: 0, tiltY: 0, state: PenSample.out)])
        default:
            break
        }
    }

    @objc private func handleLogTap() {
        onToggleLog?()
    }
}
