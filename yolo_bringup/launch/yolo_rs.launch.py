import os
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch.launch_description_sources import PythonLaunchDescriptionSource
from ament_index_python.packages import get_package_share_directory


def generate_launch_description():
    # RealSense Camera Node
    realsense_camera_node = Node(
        package="realsense2_camera",
        executable="realsense2_camera_node",
        name="camera",
        namespace="camera",
        parameters=[{
            "enable_infra1": False,
            "enable_infra2": False,
            "enable_depth": True,
            "enable_color": True,
            "align_depth.enable": True,
            "enable_sync": True,
            "enable_rgbg": True,
            "enable_gyro": False,
            "enable_accel": False,
            "depth_module.depth_profile": "640x480x30",
            "rgb_camera.color_profile": "640x480x30",
        }]
    )

    # YOLO Node (included from yolo.launch.py)
    yolo_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(
                get_package_share_directory("yolo_bringup"),
                "launch",
                "yolo.launch.py",
            )
        ),
        launch_arguments={
            "model": LaunchConfiguration("model", default="yolo8m-seg.pt"),
            "tracker": LaunchConfiguration("tracker", default="bytetrack.yaml"),
            "device": LaunchConfiguration("device", default="cuda:0"),
            "enable": LaunchConfiguration("enable", default="True"),
            "threshold": LaunchConfiguration("threshold", default="0.5"),
            "input_image_topic": LaunchConfiguration(
                "input_image_topic", default="/camera/camera/color/image_raw"
            ),
            "image_reliability": LaunchConfiguration(
                "image_reliability", default="1"
            ),
            "namespace": LaunchConfiguration("namespace", default="yolo"),
        }.items(),
    )

    return LaunchDescription([
        realsense_camera_node,
        yolo_launch,
    ])