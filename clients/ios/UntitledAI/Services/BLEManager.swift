//
//  BLEManager.swift
//  UntitledAI
//
//  Created by ethan on 1/15/24.
//

import Foundation
import CoreBluetooth

class BLEManager: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate, ObservableObject {
    
    static let shared = BLEManager()
    
    @Published var connectedDeviceName: String?
    var centralManager: CBCentralManager!
    var connectedPeripheral: CBPeripheral?
    let serviceUUID = CBUUID(string: AppConstants.bleServiceUUID)
    let audioCharacteristicUUID = CBUUID(string: AppConstants.bleAudioCharacteristicUUID)
    private var frameSequencer: FrameSequencer?
    private let socketManager = SocketManager.shared
    
    override init() {
        super.init()
        centralManager = CBCentralManager(delegate: self, queue: nil, options: [CBCentralManagerOptionRestoreIdentifierKey: "com.untitledai.restorationKey"])
    }
    
    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        print("Connected to peripheral: name=\(peripheral.name ?? ""), UUID=\(peripheral.identifier)")
        DispatchQueue.main.async {
            self.connectedDeviceName = peripheral.name
        }
        peripheral.discoverServices([serviceUUID])
        frameSequencer = FrameSequencer()
    }
    
    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        if central.state == .poweredOn {
            retrieveConnectedPeripherals()
            scanForPeripherals()
        }
    }
    
    private func retrieveConnectedPeripherals() {
        let connectedPeripherals = centralManager.retrieveConnectedPeripherals(withServices: [serviceUUID])
        for peripheral in connectedPeripherals {
            connectedPeripheral = peripheral
            connectedPeripheral!.delegate = self
            centralManager.connect(connectedPeripheral!, options: nil)
        }
    }
    
    func scanForPeripherals() {
        print("Starting scan for peripherals")
        disconnectPeripheral()
        centralManager.scanForPeripherals(withServices: [serviceUUID], options: [CBCentralManagerScanOptionAllowDuplicatesKey: NSNumber(value: true)])
    }
    
    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral, advertisementData: [String: Any], rssi RSSI: NSNumber) {
        let peripheralName = peripheral.name ?? "Unknown"
        let peripheralId = peripheral.identifier

        print("""
              Discovered Peripheral: \(peripheralName)
              Identifier: \(peripheralId)
              RSSI: \(RSSI)
              """)

        if connectedPeripheral?.identifier != peripheral.identifier {
            // New peripheral discovered, reset the old connection
            if let connected = connectedPeripheral {
                centralManager.cancelPeripheralConnection(connected)
            }
            connectedPeripheral = peripheral
            connectedPeripheral!.delegate = self
            centralManager.connect(connectedPeripheral!, options: nil)
        }
        centralManager.stopScan()
    }
    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        print("Discovered services")
        guard let services = peripheral.services else { return }
        
        for service in services {
            peripheral.discoverCharacteristics(nil, for: service)
        }
    }
    
    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        print("Discovered characteristics")
        print(service.characteristics)
        guard let characteristics = service.characteristics else { return }
        for characteristic in characteristics {
            if characteristic.uuid.isEqual(audioCharacteristicUUID) {
                if characteristic.properties.contains(.notify) {
                    print("Subscribing to characteristic \(characteristic.uuid)")
                    peripheral.setNotifyValue(true, for: characteristic)
                } else {
                    print("Characteristic does not support notifications")
                }
            }
        }
    }
    
    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        if let error = error {
            print("Error updating value: \(error.localizedDescription)")
            return
        }
        
        guard let value = characteristic.value else {
            print("No value received")
            return
        }
        
        if characteristic.uuid.isEqual(audioCharacteristicUUID) {
            // Retrieve the current capture or create a new one
            let capture = CaptureManager.shared.getCurrentCapture() ?? {
                let deviceName = peripheral.name ?? "Unknown"
                let newCapture = Capture(deviceName: deviceName)
                CaptureManager.shared.createCapture(capture: newCapture)
                print("Created new capture for device: \(deviceName)")
                return newCapture
            }()

            if let completeFrames = frameSequencer?.add(packet: value) {
                for frame in completeFrames {
                    socketManager.sendAudioData(frame, capture: capture)
                    // TODO: append to writer
                }
            }
        }
    }
    
    func centralManager(_ central: CBCentralManager, willRestoreState dict: [String: Any]) {
        if let restoredPeripherals = dict[CBCentralManagerRestoredStatePeripheralsKey] as? [CBPeripheral] {
            for peripheral in restoredPeripherals {
                connectedPeripheral = peripheral
                connectedPeripheral!.delegate = self
                
                if peripheral.state == .connected {
                    peripheral.discoverServices([serviceUUID])
                } else if peripheral.state == .disconnected {
                    centralManager.connect(peripheral, options: nil)
                }
            }
        }
    }
    
    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        if let error = error {
            print("Error changing notification state: \(error.localizedDescription)")
        } else {
            print("Notification state updated for \(characteristic.uuid): \(characteristic.isNotifying)")
        }
    }
    
    func disconnectPeripheral() {
        if let peripheral = connectedPeripheral {
            centralManager.cancelPeripheralConnection(peripheral)
            connectedPeripheral = nil
        }
    }
    
    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        print("Peripheral disconnected")
        if peripheral == connectedPeripheral {
            DispatchQueue.main.async {
                self.connectedDeviceName = nil
            }
            connectedPeripheral = nil

            if let capture = CaptureManager.shared.getCurrentCapture() {
                socketManager.finishAudio(capture: capture)
                CaptureManager.shared.endCapture()
            }
            
            // Restart scanning for other peripherals
            scanForPeripherals()
        }
    }
  
}