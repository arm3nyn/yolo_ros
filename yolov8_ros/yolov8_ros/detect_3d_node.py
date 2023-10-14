# Copyright (C) 2023  Miguel Ángel González Santamarta

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import time
from typing import Tuple

import message_filters
import numpy as np
import rclpy
from cv_bridge import CvBridge
from geometry_msgs.msg import TransformStamped
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CameraInfo, Image
from tf2_ros import TransformException
from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener

from yolov8_msgs.msg import (BoundingBox3D, Detection, DetectionArray,
                             KeyPoint3D, KeyPoint3DArray)


class Detect3DNode(Node):

    def __init__(self) -> None:
        super().__init__("bbox3d_node")

        # parameters
        self.declare_parameter("target_frame", "base_link")
        self.target_frame = self.get_parameter(
            "target_frame").get_parameter_value().string_value
        self.declare_parameter("maximum_detection_threshold", 0.3)
        self.maximum_detection_threshold = self.get_parameter(
            "maximum_detection_threshold").get_parameter_value().double_value
        self.declare_parameter("depth_image_units_divisor", 1000)
        self.depth_image_units_divisor = self.get_parameter(
            "depth_image_units_divisor").get_parameter_value().integer_value

        # aux
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        self.cv_bridge = CvBridge()

        # pubs
        self._pub = self.create_publisher(DetectionArray, "detections_3d", 10)

        # subs
        self.depth_sub = message_filters.Subscriber(
            self, Image, "depth_image",
            qos_profile=qos_profile_sensor_data)
        self.depth_info_sub = message_filters.Subscriber(
            self, CameraInfo, "depth_info",
            qos_profile=qos_profile_sensor_data)
        self.detections_sub = message_filters.Subscriber(
            self, DetectionArray, "detections")

        self._synchronizer = message_filters.ApproximateTimeSynchronizer(
            (self.depth_sub, self.depth_info_sub, self.detections_sub), 10, 0.5)
        self._synchronizer.registerCallback(self.on_detections)

    def on_detections(self,
                      depth_msg: Image,
                      depth_info_msg: CameraInfo,
                      detections_msg: DetectionArray,
                      ) -> None:

        t1 = time.time()
        
        # check if there are detections
        if not detections_msg.detections:
            return

        transform = self.get_transform(depth_info_msg.header.frame_id)

        if transform is None:
            return

        depth_image = self.cv_bridge.imgmsg_to_cv2(depth_msg)
        new_detections_msg = DetectionArray()
        new_detections_msg.header = detections_msg.header

        for detection in detections_msg.detections:
            bbox3d = self.convert_bb_to_3d(
                depth_image, depth_info_msg, detection)

            if bbox3d is not None:
                new_detections_msg.detections.append(detection)

                bbox3d = Detect3DNode.transform_3d_box(
                    bbox3d, transform[0], transform[1])
                bbox3d.frame_id = self.target_frame
                new_detections_msg.detections[-1].bbox3d = bbox3d

                if detection.keypoints.data:
                    keypoints3d = self.convert_keypoints_to_3d(
                        depth_image, depth_info_msg, detection)
                    keypoints3d = Detect3DNode.transform_3d_keypoints(
                        keypoints3d, transform[0], transform[1])
                    keypoints3d.frame_id = self.target_frame
                    new_detections_msg.detections[-1].keypoints3d = keypoints3d

        self._pub.publish(new_detections_msg)

        t2 = time.time()
        with open('timings.txt', "a+") as file:
            file.write(f"{(t2 - t1) * 1000:.1f}" + "\n")

    def convert_bb_to_3d(self,
                         depth_image: np.ndarray,
                         depth_info: CameraInfo,
                         detection: Detection
                         ) -> BoundingBox3D:

        # crop depth image by the 2d BB
        center_x = int(detection.bbox.center.position.x)
        center_y = int(detection.bbox.center.position.y)
        size_x = int(detection.bbox.size.x)
        size_y = int(detection.bbox.size.y)
        u_min, u_max = max(center_x - size_x // 2, 0), min(center_x + size_x // 2, depth_image.shape[1] - 1)
        v_min, v_max = max(center_y - size_y // 2, 0), min(center_y + size_y // 2, depth_image.shape[0] - 1)
        roi = depth_image[v_min:v_max, u_min:u_max] / self.depth_image_units_divisor  # convert to meters
        if not np.any(roi):
            return None
        self.get_logger().info(f"{self.depth_image_units_divisor = }")

        # find the z coordinate on the 3D BB
        z_mean = np.nanmean(np.where(roi == 0, np.nan, roi))
        z_diff = np.abs(roi - z_mean)
        mask_z = z_diff <= self.maximum_detection_threshold
        if not np.any(mask_z):
            return None
        roi_threshold = roi[mask_z]
        z_min, z_max = np.min(roi_threshold), np.max(roi_threshold)
        z = (z_max + z_min) / 2 
            
        # project from image to world space
        k = depth_info.k
        px, py, fx, fy = k[2], k[5], k[0], k[4]
        x = z * (center_x - px) / fx
        y = z * (center_y - py) / fy
        w = z * (size_x / fx)
        h = z * (size_y / fy)

        # create 3D BB
        msg = BoundingBox3D()
        msg.center.position.x = x
        msg.center.position.y = y
        msg.center.position.z = z
        msg.size.x = w
        msg.size.y = h
        msg.size.z = (z_max - z_min)

        return msg

    def convert_keypoints_to_3d(self,
                                depth_image: np.ndarray,
                                depth_info: CameraInfo,
                                detection: Detection
                                ) -> KeyPoint3DArray:

        # Build an array of 2d keypoints
        keypoints_2d = np.array([[p.point.x, p.point.y] for p in detection.keypoints.data], dtype=np.int16)
        u = np.array(keypoints_2d[:, 1]).clip(0, depth_info.height - 1)
        v = np.array(keypoints_2d[:, 0]).clip(0, depth_info.width - 1)

        # sample depth image and project to 3D
        z = depth_image[u, v]
        k = depth_info.k
        px, py, fx, fy = k[2], k[5], k[0], k[4]
        x = z * (v - px) / fx
        y = z * (u - py) / fy
        points_3d = np.dstack([x, y, z]).reshape(-1, 3) / self.depth_image_units_divisor  # convert to meters

        # generate message
        msg_array = KeyPoint3DArray()
        for p, d in zip(points_3d, detection.keypoints.data):
            if not np.isnan(p).any():
                msg = KeyPoint3D()
                msg.point.x = p[0]
                msg.point.y = p[1]
                msg.point.z = p[2]
                msg.id = d.id
                msg.score = d.score
                msg_array.data.append(msg)

        return msg_array

    def get_transform(self, frame_id: str) -> Tuple[np.ndarray]:
        # transform position from point cloud frame to target_frame
        rotation = None
        translation = None

        try:
            transform: TransformStamped = self.tf_buffer.lookup_transform(
                self.target_frame,
                frame_id,
                rclpy.time.Time())

            translation = np.array([transform.transform.translation.x,
                                    transform.transform.translation.y,
                                    transform.transform.translation.z])

            rotation = np.array([transform.transform.rotation.w,
                                 transform.transform.rotation.x,
                                 transform.transform.rotation.y,
                                 transform.transform.rotation.z])

            return translation, rotation

        except TransformException as ex:
            self.get_logger().error(f"Could not transform: {ex}")
            return None

    @staticmethod
    def transform_3d_box(
        bbox: BoundingBox3D,
        translation: np.ndarray,
        rotation: np.ndarray
    ) -> BoundingBox3D:

        # position
        position = Detect3DNode.qv_mult(
            rotation,
            np.array([bbox.center.position.x,
                      bbox.center.position.y,
                      bbox.center.position.z])
        ) + translation

        bbox.center.position.x = position[0]
        bbox.center.position.y = position[1]
        bbox.center.position.z = position[2]

        # size
        size = Detect3DNode.qv_mult(
            rotation,
            np.array([bbox.size.x,
                      bbox.size.y,
                      bbox.size.z])
        )

        bbox.size.x = abs(size[0])
        bbox.size.y = abs(size[1])
        bbox.size.z = abs(size[2])

        return bbox

    @staticmethod
    def transform_3d_keypoints(
        keypoints: KeyPoint3DArray,
        translation: np.ndarray,
        rotation: np.ndarray,
    ) -> KeyPoint3DArray:

        for point in keypoints.data:
            position = Detect3DNode.qv_mult(
                rotation,
                np.array([
                    point.point.x,
                    point.point.y,
                    point.point.z
                ])
            ) + translation

            point.point.x = position[0]
            point.point.y = position[1]
            point.point.z = position[2]

        return keypoints

    @staticmethod
    def qv_mult(q: np.ndarray, v: np.ndarray) -> np.ndarray:
        q = np.array(q, dtype=np.float64)
        v = np.array(v, dtype=np.float64)
        qvec = q[1:]
        uv = np.cross(qvec, v)
        uuv = np.cross(qvec, uv)
        return v + 2 * (uv * q[0] + uuv)


def main():
    rclpy.init()
    rclpy.spin(Detect3DNode())
    rclpy.shutdown()
