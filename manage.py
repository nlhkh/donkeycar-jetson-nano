#!/usr/bin/env python3
"""
Scripts to drive a donkey 2 car and train a model for it.

Usage:
    manage.py (drive) [--model=<model>] [--js] [--chaos] [--type=<linear|linear-dropout|linear-cropped-dropout|rnn>]
    manage.py (train) [--tub=<tub1,tub2,..tubn>]  (--model=<model>) [--type=<linear|linear-dropout|rnn>] [--base_model=<base_model>] [--no_cache]

Options:
    -h --help        Show this screen.
    --tub TUBPATHS   List of paths to tubs. Comma separated. Use quotes to use wildcards. ie "~/tubs/*"
    --chaos          Add periodic random steering when manually driving
"""
import os

from docopt import docopt
import donkeycar as dk

# from donkeycar.parts.camera import PiCamera
from camera import RealSenseCamera
from donkeycar.parts.transform import Lambda
from donkeycar.parts.keras import KerasLinear
from donkeycar.parts.actuator import PCA9685, PWMSteering, PWMThrottle
from donkeycar.parts.datastore import TubGroup, TubWriter
from donkeycar.parts.clock import Timestamp


def drive(cfg, model_path=None, model_type='linear-dropout', use_joystick=False, use_chaos=False):

    """
    Construct a working robotic vehicle from many parts.
    Each part runs as a job in the Vehicle loop, calling either
    it's run or run_threaded method depending on the constructor flag `threaded`.
    All parts are updated one after another at the framerate given in
    cfg.DRIVE_LOOP_HZ assuming each part finishes processing in a timely manner.
    Parts may have named outputs and inputs. The framework handles passing named outputs
    to parts requesting the same named input.
    """

    V = dk.vehicle.Vehicle()

    clock = Timestamp()
    V.add(clock, outputs=['timestamp'])

    # cam = PiCamera(resolution=cfg.CAMERA_RESOLUTION)
    cam = RealSenseCamera()
    V.add(cam, outputs=['cam/image_array'], threaded=True)

    if use_joystick:
        from donkeypart_ps3_controller.part import PS4JoystickController

        ctr = PS4JoystickController(throttle_scale=cfg.JOYSTICK_MAX_THROTTLE,
                                    steering_scale=cfg.JOYSTICK_STEERING_SCALE,
                                    auto_record_on_throttle=cfg.AUTO_RECORD_ON_THROTTLE)
    else:
        from donkeycar.parts.web_controller import LocalWebController

        ctr = LocalWebController(use_chaos=use_chaos)

    V.add(ctr,
          inputs=['cam/image_array'],
          outputs=['user/angle', 'user/throttle', 'user/mode', 'recording'],
          threaded=True)

    # See if we should even run the pilot module.
    # This is only needed because the part run_condition only accepts boolean
    def pilot_condition(mode):
        if mode == 'user':
            return False
        else:
            return True

    pilot_condition_part = Lambda(pilot_condition)
    V.add(pilot_condition_part,
          inputs=['user/mode'],
          outputs=['run_pilot'])

    # Run the pilot if the mode is not user.
    kl = get_model(model_type) 
    if model_path:
        kl.load(model_path)

    V.add(kl,
          inputs=['cam/image_array'],
          outputs=['pilot/angle', 'pilot/throttle'],
          run_condition='run_pilot')

    # Choose what inputs should change the car.
    def drive_mode(mode,
                   user_angle, user_throttle,
                   pilot_angle, pilot_throttle):
        if mode == 'user':
            return user_angle, user_throttle

        elif mode == 'local_angle':
            return pilot_angle, user_throttle

        else:
            return pilot_angle, pilot_throttle

    drive_mode_part = Lambda(drive_mode)
    V.add(drive_mode_part,
          inputs=['user/mode', 'user/angle', 'user/throttle',
                  'pilot/angle', 'pilot/throttle'],
          outputs=['angle', 'throttle'])

    steering_controller = PCA9685(cfg.STEERING_CHANNEL)
    steering = PWMSteering(controller=steering_controller,
                           left_pulse=cfg.STEERING_LEFT_PWM,
                           right_pulse=cfg.STEERING_RIGHT_PWM) 

    throttle_controller = PCA9685(cfg.THROTTLE_CHANNEL)
    throttle = PWMThrottle(controller=throttle_controller,
                           max_pulse=cfg.THROTTLE_FORWARD_PWM,
                           zero_pulse=cfg.THROTTLE_STOPPED_PWM,
                           min_pulse=cfg.THROTTLE_REVERSE_PWM)

    V.add(steering, inputs=['angle'])
    V.add(throttle, inputs=['throttle'])

    # add tub to save data
    inputs = ['cam/image_array', 'user/angle', 'user/throttle', 'user/mode', 'timestamp']
    types = ['image_array', 'float', 'float',  'str', 'str']

    # multiple tubs
    # th = TubHandler(path=cfg.DATA_PATH)
    # tub = th.new_tub_writer(inputs=inputs, types=types)

    # single tub
    tub = TubWriter(path=cfg.TUB_PATH, inputs=inputs, types=types)
    V.add(tub, inputs=inputs, run_condition='recording')

    # run the vehicle
    V.start(rate_hz=cfg.DRIVE_LOOP_HZ,
            max_loop_count=cfg.MAX_LOOPS)


def train(cfg, tub_names, new_model_path, model_type='linear-dropout', base_model_path=None):
    """
    use the specified data in tub_names to train an artifical neural network
    saves the output trained model as model_name
    """
    X_keys = ['cam/image_array']
    y_keys = ['user/angle', 'user/throttle']

    new_model_path = os.path.expanduser(new_model_path)

    kl = get_model(model_type) 
    if base_model_path is not None:
        base_model_path = os.path.expanduser(base_model_path)
        kl.load(base_model_path)

    print('tub_names', tub_names)
    if not tub_names:
        tub_names = os.path.join(cfg.DATA_PATH, '*')
    tubgroup = TubGroup(tub_names)
    train_gen, val_gen = tubgroup.get_train_val_gen(X_keys, y_keys,
                                                    batch_size=cfg.BATCH_SIZE,
                                                    train_frac=cfg.TRAIN_TEST_SPLIT)

    total_records = len(tubgroup.df)
    total_train = int(total_records * cfg.TRAIN_TEST_SPLIT)
    total_val = total_records - total_train
    print('train: %d, validation: %d' % (total_train, total_val))
    steps_per_epoch = total_train // cfg.BATCH_SIZE
    print('steps_per_epoch', steps_per_epoch)

    kl.train(train_gen,
             val_gen,
             saved_model_path=new_model_path,
             steps=steps_per_epoch,
             train_split=cfg.TRAIN_TEST_SPLIT)


def get_model(model_type):
    if model_type == 'linear':
        from models import default_linear
        from donkeycar.parts.keras import KerasLinear
        return KerasLinear(model=default_linear())
    elif model_type == 'linear-dropout':
        from models import linear_dropout
        from donkeycar.parts.keras import KerasLinear
        return KerasLinear(model=linear_dropout())
    elif model_type == 'linear-cropped-dropout':
        from models import linear_cropped_dropout
        from donkeycar.parts.keras import KerasLinear
        return KerasLinear(model=linear_cropped_dropout())
    elif model_type == 'rnn':
        from models import KerasRNN_LSTM
        return KerasRNN_LSTM()
    else:
        raise ValueError('Unknown model type')


if __name__ == '__main__':
    args = docopt(__doc__)
    cfg = dk.load_config()

    if args['drive']:
        drive(cfg, model_path=args['--model'], model_type=args['--type'], use_joystick=args['--js'], use_chaos=args['--chaos'])

    elif args['train']:
        tub = args['--tub']
        new_model_path = args['--model']
        model_type=args['--type']
        base_model_path = args['--base_model']
        cache = not args['--no_cache']
        train(cfg, tub, new_model_path, model_type, base_model_path)


