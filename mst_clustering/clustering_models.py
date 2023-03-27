import math
import ctypes
import numpy as np

from numpy import ndarray
from itertools import product
from scipy.spatial import KDTree
from abc import ABC, abstractmethod
from concurrent.futures import wait, ALL_COMPLETED

from mst_clustering.multiprocessing_tools import SharedMemoryPool, submittable
from mst_clustering.cpp_adapters import SpanningForest, Edge
from multiprocessing.sharedctypes import RawArray, RawValue
from mst_clustering.math_utils import hyper_volume


class ClusteringModel(ABC):
    @abstractmethod
    def __call__(self, data: ndarray, forest: SpanningForest, workers: int = 1, partition: ndarray = None) -> ndarray:
        pass

    @staticmethod
    def get_cluster_info(data: ndarray, forest: SpanningForest, cluster_idx: int) -> (ndarray, list, ndarray):
        root = forest.get_roots()[cluster_idx]
        cluster_edges = forest.get_edges(root)

        if not cluster_edges:
            cluster_ids = np.array([root])
            cluster_center = data[cluster_ids.squeeze()]
        else:
            cluster_ids = np.unique(list(map(lambda edge: [edge.first_node, edge.second_node], cluster_edges)))
            cluster_center = np.mean(data[cluster_ids], axis=0)

        return cluster_ids, cluster_edges, cluster_center


class ZahnModel(ClusteringModel):
    cutting_cond: float
    hv_condition: float
    weighting_exp: float
    num_of_clusters: int
    use_first_criterion: bool
    use_third_criterion: bool
    use_second_criterion: bool

    __kdtree: KDTree or None

    def __init__(self, cutting_condition=2.5, weighting_exponent=2, hv_condition=1e-4, max_num_of_clusters: int = -1,
                 use_first_criterion: bool = True, use_second_criterion: bool = True,
                 use_third_criterion: bool = True):
        self.cutting_cond = cutting_condition
        self.weighting_exp = weighting_exponent
        self.hv_condition = hv_condition
        self.num_of_clusters = max_num_of_clusters
        self.use_first_criterion = use_first_criterion
        self.use_second_criterion = use_second_criterion
        self.use_third_criterion = use_third_criterion
        self.__kdtree = None

    def __call__(self, data: ndarray, forest: SpanningForest, workers: int = 1, partition: ndarray = None) -> ndarray:
        shared_memory_dict = dict({
            "shared_data": RawArray(ctypes.c_double, data.flatten()),
            "shared_rows_count": RawValue(ctypes.c_int32, data.shape[0]),
            "shared_weighting_exponent": RawValue(ctypes.c_double, self.weighting_exp)
        })

        with SharedMemoryPool(max_workers=workers, shared_memory_dict=shared_memory_dict) as pool:
            while self._check_num_of_clusters(forest):
                info = map(lambda c: ZahnModel.get_cluster_info(data, forest, c), range(forest.size))
                futures = list(pool.submit(ZahnModel.__fuzzy_hyper_volume_task, ids, center) for ids, _, center in info)

                wait(futures, return_when=ALL_COMPLETED)

                volumes = np.fromiter(map(lambda future: future.result(), futures), dtype=np.float64)
                volumes_without_noise = np.where(volumes == math.inf, -1, volumes)

                bad_cluster_edges = forest.get_edges(forest.get_roots()[np.argmax(volumes_without_noise)])

                all_edges = forest.get_edges(forest.get_roots()[0])
                weights = np.fromiter(map(lambda edge: edge.weight, bad_cluster_edges), dtype=np.float64)
                max_weight_idx = int(np.argmax(weights))
                max_weight = weights[max_weight_idx]

                worst_edge_found = False
                if self.use_first_criterion and self._check_first_criterion(data, all_edges, max_weight):
                    worst_edge = bad_cluster_edges[max_weight_idx]
                    worst_edge_found = True
                if not worst_edge_found and self.use_second_criterion:
                    if self.__kdtree is None:
                        self.__kdtree = KDTree(data)
                    index = self._check_second_criterion(data, all_edges, bad_cluster_edges, weights, workers=workers)
                    if index != -1:
                        worst_edge = bad_cluster_edges[index]
                        worst_edge_found = True
                if not worst_edge_found and self.use_third_criterion:
                    worst_edge = self._check_third_criterion(data, bad_cluster_edges)
                    if worst_edge is not None:
                        worst_edge_found = True
                if not worst_edge_found:
                    break

                forest.remove_edge(worst_edge.first_node, worst_edge.second_node)

        partition = np.zeros((forest.size, data.shape[0]))
        for cluster in range(forest.size):
            cluster_ids, *_ = ZahnModel.get_cluster_info(data, forest, cluster)
            partition[cluster, cluster_ids] = 1

        return partition

    def _check_num_of_clusters(self, forest: SpanningForest) -> bool:
        return self.num_of_clusters == -1 or forest.size < self.num_of_clusters

    def _check_first_criterion(self, data: ndarray, all_edges: list, edge_weight: float) -> bool:
        criterion = self.cutting_cond * sum(map(lambda edge: edge.weight, all_edges)) / (data.shape[0] - 1)
        return edge_weight >= criterion

    def _check_second_criterion(self, data: ndarray, all_edges: list, bad_cluster_edges: list, edges_weights: ndarray,
                                workers: int) -> int:
        sorted_indices = np.argsort(edges_weights)[::-1]
        for index in sorted_indices:
            first_node = bad_cluster_edges[index].first_node
            second_node = bad_cluster_edges[index].second_node
            first_neighbours = self.__kdtree.query_ball_point(x=data[first_node], r=edges_weights[index],
                                                              workers=workers)
            first_edges = list(filter(
                lambda edge: ((edge.first_node in first_neighbours) or (edge.second_node in first_neighbours)) and (
                        edge.first_node != first_node and edge.second_node != second_node), all_edges))
            second_neighbours = self.__kdtree.query_ball_point(x=data[second_node], r=edges_weights[index],
                                                               workers=workers)
            second_edges = list(filter(
                lambda edge: ((edge.first_node in second_neighbours) or (edge.second_node in second_neighbours)) and (
                        edge.first_node != first_node and edge.second_node != second_node), all_edges))
            first_edges.extend(second_edges)

            weight = edges_weights[index]
            if len(first_edges) < 2:
                continue
            criterion = self.cutting_cond * sum(map(lambda edge: edge.weight, first_edges)) / (len(first_edges) - 1)
            if weight >= criterion:
                return index

        return -1

    def _check_third_criterion(self, data: ndarray, cluster_edges: list) -> Edge or None:
        bad_edge_index = None

        temp_forest = SpanningForest(size=data.shape[0])
        for cluster_edge in cluster_edges:
            temp_forest.add_edge(cluster_edge.first_node, cluster_edge.second_node, cluster_edge.weight)

        min_total_hv = math.inf
        for edge_index, cluster_edge in enumerate(cluster_edges):
            temp_forest.remove_edge(cluster_edge.first_node, cluster_edge.second_node)

            roots = temp_forest.get_roots()

            left_root = temp_forest.find_root(cluster_edge.first_node)
            left_cluster_ids, _, cluster_center = ZahnModel.get_cluster_info(data, temp_forest, roots.index(left_root))
            left_hv = hyper_volume(data, self.weighting_exp, left_cluster_ids, cluster_center)

            right_root = temp_forest.find_root(cluster_edge.second_node)
            right_cluster_ids, _, cluster_center = self.get_cluster_info(data, temp_forest, roots.index(right_root))
            right_hv = hyper_volume(data, self.weighting_exp, right_cluster_ids, cluster_center)

            if not (left_hv is math.inf or right_hv is math.inf):
                total_hv = left_hv + right_hv
                if total_hv <= min_total_hv:
                    bad_edge_index = edge_index
                    min_total_hv = total_hv

            temp_forest.add_edge(cluster_edge.first_node, cluster_edge.second_node, cluster_edge.weight)

        return cluster_edges[bad_edge_index] if min_total_hv > self.hv_condition and min_total_hv != math.inf \
            else None

    @staticmethod
    @submittable
    def __fuzzy_hyper_volume_task(cluster_ids: ndarray, cluster_center: ndarray) -> float:
        import numpy as np
        from mst_clustering.math_utils import hyper_volume

        shared_memory = SharedMemoryPool.get_shared_memory()
        shared_data = shared_memory["shared_data"]
        shared_rows_count = shared_memory["shared_rows_count"]
        shared_weighting_exponent = shared_memory["shared_weighting_exponent"]

        data = np.frombuffer(shared_data).reshape((shared_rows_count.value, -1))
        weighting_exponent = shared_weighting_exponent.value
        volume = hyper_volume(data, weighting_exponent, cluster_ids, cluster_center)

        return volume


class GathGevaModel(ClusteringModel):
    termination_tolerance: float
    weighting_exp: float

    def __init__(self, termination_tolerance: float = 1e-4, weighting_exponent: float = 2):
        self.termination_tolerance = termination_tolerance
        self.weighting_exp = weighting_exponent

    def __call__(self, data: ndarray, forest: SpanningForest, workers: int = 1, partition: ndarray = None) -> ndarray:
        assert partition is not None, "This clustering method requires a non None partition matrix."

        non_noise = ~np.all(partition == 0, axis=1)
        non_noise_clusters = np.arange(partition.shape[0])[non_noise]

        while True:
            previous_partition = partition.copy()
            power = 2 / (self.weighting_exp - 1)

            ln_distance_matrix = self._get_ln_distance_matrix(data, partition, non_noise_clusters, workers)

            for cluster, point_idx in product(non_noise_clusters, np.arange(partition.shape[1])):
                ln_distance = ln_distance_matrix[cluster, point_idx]

                new_partition = 0.0
                for other_cluster in non_noise_clusters:
                    new_partition += np.exp((ln_distance - ln_distance_matrix[other_cluster, point_idx]) * power)
                new_partition **= -1

                partition[cluster, point_idx] = new_partition

            partitions_distance = np.linalg.norm(partition - previous_partition)
            if partitions_distance < self.termination_tolerance:
                break

        return partition

    def _get_ln_distance_matrix(self, data: ndarray, partition: ndarray, non_noise_clusters: ndarray,
                                workers: int) -> ndarray:
        shared_memory_dict = dict({
            "shared_data": RawArray(ctypes.c_double, data.flatten()),
            "shared_partition": RawArray(ctypes.c_double, partition.flatten()),
            "shared_rows_count": RawValue(ctypes.c_int32, data.shape[0]),
            "shared_clusters_count": RawValue(ctypes.c_int32, partition.shape[0]),
            "shared_weighting_exponent": RawValue(ctypes.c_double, self.weighting_exp)
        })

        with SharedMemoryPool(max_workers=workers, shared_memory_dict=shared_memory_dict) as pool:
            futures = [pool.submit(GathGevaModel.__compute_distances_task, cluster) for cluster in non_noise_clusters]

            wait(futures, return_when=ALL_COMPLETED)

            distance_matrix = np.zeros_like(partition)
            distance_matrix[non_noise_clusters] = list(map(lambda future: future.result(), futures))

        return distance_matrix

    @staticmethod
    @submittable
    def __compute_distances_task(cluster: int) -> ndarray:
        import numpy as np
        from mst_clustering.math_utils import cluster_ln_distances

        shared_memory = SharedMemoryPool.get_shared_memory()
        shared_data = shared_memory["shared_data"]
        shared_partition = shared_memory["shared_partition"]
        shared_rows_count = shared_memory["shared_rows_count"]
        shared_clusters_count = shared_memory["shared_clusters_count"]
        shared_weighting_exponent = shared_memory["shared_weighting_exponent"]

        data = np.frombuffer(shared_data).reshape((shared_rows_count.value, -1))
        weighting_exponent = shared_weighting_exponent.value
        partition = np.frombuffer(shared_partition).reshape((shared_clusters_count.value, -1))
        ln_distances = cluster_ln_distances(data, weighting_exponent, partition, cluster)

        return ln_distances
