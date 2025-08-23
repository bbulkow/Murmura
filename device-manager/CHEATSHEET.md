# when we're on the playa we need it to be as simple as possible! The full doc is a bit much

## scan

./device_scanner.py -n 192.168.12.0/24 --action create
 -> or add, update

 ./id_manager.py -c provision-single --network 192.168.12.0/24 --new-id SOMETHING

 ./device_controller --id SOMETHING --command status
 ./device_controller --id SOMETHING --command status
 ./device_controller --id SOMETHING --command save-config
 ./device_controller --id SOMETHING --new-id SOMETHING_ELSE

 ./device_controller --id SOMETHING -c set-volume --track 0 --volume 50
 ./device_controller --id SOMETHING -c set-volume --global --volume 75

 ./device_controller --id SOMETHING -c set-file --track 0 --filename osprey01.wav

 ./device_controller --id SOMETHING --command reboot