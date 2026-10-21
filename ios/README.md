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

## Building on a Mac

1. Xcode → New Project → iOS App, name `TerraForge`, interface SwiftUI.
2. Delete the generated `ContentView.swift` and app file; drag the files from this folder in.
3. Info.plist keys:
   - `NSCameraUsageDescription` = "Used to capture the LiDAR scan"
   - `NSLocationWhenInUseUsageDescription` = "Used to georeference the scan"
   - For a local test server over http, add `NSAppTransportSecurity` → `NSAllowsLocalNetworking` = YES
4. Select your iPhone as the run target (the simulator has no LiDAR), set a signing team and run.
5. If Xcode reports Swift 6 concurrency errors (e.g. "non-Sendable CVPixelBuffer"), set
   Build Settings → *Swift Language Version* to **Swift 5**.

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
