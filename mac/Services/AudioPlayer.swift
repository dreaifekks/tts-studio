import AVFoundation
import Observation

@MainActor @Observable
final class AudioPlayer: NSObject, AVAudioPlayerDelegate {
    private var player: AVAudioPlayer?
    private(set) var currentID: String?
    private(set) var playing = false
    var volume: Float = 1
    func play(id: String, data: Data) throws {
        player?.stop()
        let audio = try AVAudioPlayer(data: data)
        audio.delegate = self
        audio.volume = volume
        audio.prepareToPlay()
        guard audio.play() else { throw APIError(status: 0, message: "音频无法播放") }
        player = audio
        currentID = id
        playing = true
    }
    func toggle() {
        guard let player else { return }
        if player.isPlaying { player.pause(); playing = false }
        else { playing = player.play() }
    }
    func stop() { player?.stop(); playing = false }
    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in self.playing = false }
    }
}
