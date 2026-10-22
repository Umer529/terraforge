# TerraForge iOS capture app (Swift, ARKit, RealityKit)

Source for the field-operator app. It needs a Mac with Xcode 15+ to build and
an iPhone/iPad with a LiDAR sensor (iPhone 12 Pro or later Pro models) to run.

| File | Module in the report |
|---|---|
| `CaptureController.swift` | Capture module: ARKit LiDAR mesh (vertices + faces), **automatic photos** every 0.5 m / 20° with their ARKit pose and intrinsics (for photo + LiDAR fusion on the server), location module (Core Location) |
| `SessionStore.swift` | Local storage: offline sessions on the phone, packaging to zip |
| `APIClient.swift` | Packaging & upload module: token login, SHA-256 chunked upload with resume |
| `Views.swift` | Screens: Log in, Scan Library, Capture, Review and Upload |
| `Models.swift` | Manifest, GPS fix, upload record |

## Project prepared on Windows

`TerraForge.xcodeproj` is included in this folder. It already lists the six Swift files, sets Swift 5 and iOS 17, and includes `Info.plist` with camera, location, local-network, and LiDAR requirements. Open this project directly in Xcode; do not create a second project or drag the files in again.

## One-command iPhone build

The builder needs a Mac with Xcode installed and the paid Apple Developer team already signed in to Xcode. The team owner must authorize that Mac/account and provide the 10-character Team ID. The project bundle ID is `com.umer529.terraforge`; if that identifier is already taken under the team, the team owner must change it in `App.xcconfig` before building.

After cloning the repo, the builder runs:

```sh
cd terraforge/ios
chmod +x build-iphone.sh
./build-iphone.sh YOUR_TEAM_ID
```

The signed Xcode archive is written to `ios/build/TerraForge.xcarchive`. The builder can send that archive to the paid-account owner. In Xcode Organizer, the owner can distribute it to TestFlight, or export an Ad Hoc IPA after registering the target iPhone's UDID in the developer account. A raw `.app` or `.xcarchive` cannot simply be tapped on an iPhone to install it. For TestFlight, the owner uploads the archive and invites the tester; for Ad Hoc, the phone must be registered and the exported IPA installed using Apple Configurator or a Mac.

The account owner must do the initial team authorization and final distribution. Do not share their Apple password or signing certificates. Xcode, the iOS SDK, and Apple signing only run on macOS.

## Package format sent to the server

```
<session>/manifest.json          title, captured_at, device_model, up_axis = "y", scan_file = "mesh.ply"
<session>/location_fixes.csv     timestamp,latitude,longitude,altitude,horizontal_accuracy,cam_x,cam_y,cam_z
<session>/mesh.ply               LiDAR mesh (vertices + triangles) in the ARKit world frame
<session>/photos/img_0001.jpg    camera photo, sensor orientation
<session>/photos/img_0001.json   transform_column_major (camera-to-world), intrinsics, width, height
```

The server projects every photo onto the LiDAR mesh using these poses and builds a photo-coloured
3D model (see `core/pipeline.py`, stage "fuse photos").


Until the app is built, `tools/phone_upload.py` uploads packages using exactly this protocol, and
raw exports from the 3d Scanner App / Polycam can be uploaded from the dashboard.
