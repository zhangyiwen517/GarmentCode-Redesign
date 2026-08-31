# 一、按这些函数名定位
主要找这个文件：

```plain
pygarment/meshgen/boxmeshgen.py
```

在官方原项目中，`BoxMesh.load()` 的流程是：

```python
self.load_panels()
self.gen_panel_meshes()
self.collapse_stitch_vertices()
self.finalise_mesh()
self.loaded = True
```

只需要把它改成：

```python
self.load_panels()
self.gen_panel_meshes()
self.collapse_stitch_vertices()
self.finalise_mesh()

# 新增：普通边界缝合完成后，再处理贴袋到衣片内部的缝合
self.apply_surface_stitches()

self.loaded = True
```

定位方法不是看行号，而是搜索：

```plain
class BoxMesh
def load(self)
self.finalise_mesh()
```

一定要放在：

```python
self.finalise_mesh()
```

之后、`self.loaded = True` 之前。

原因是此时所有裁片都已经三角网格化，普通缝线也已经折叠完，贴袋顶点和裤片内部顶点才都有稳定的全局编号。

---

# 二、下装模块要额外输出什么
现在已经生成了贴袋 panel，因此不要重新生成贴袋，只需要在最终 specification JSON 中多写一个自定义字段：

```python
pattern["surface_stitches"]
```

例如：

```json
{
  "surface_stitches": [
    {
      "source": {
        "panel": "left_patch_pocket",
        "edge": 0
      },
      "target": {
        "panel": "pant_f_l",
        "segment": [
          [12.0, 35.0],
          [24.0, 35.0]
        ],
        "coordinate_space": "target_panel_2d"
      },
      "type": "patch_pocket_surface_stitch"
    },
    {
      "source": {
        "panel": "left_patch_pocket",
        "edge": 1
      },
      "target": {
        "panel": "pant_f_l",
        "segment": [
          [24.0, 35.0],
          [24.0, 48.0]
        ],
        "coordinate_space": "target_panel_2d"
      },
      "type": "patch_pocket_surface_stitch"
    },
    {
      "source": {
        "panel": "left_patch_pocket",
        "edge": 3
      },
      "target": {
        "panel": "pant_f_l",
        "segment": [
          [12.0, 48.0],
          [12.0, 35.0]
        ],
        "coordinate_space": "target_panel_2d"
      },
      "type": "patch_pocket_surface_stitch"
    }
  ]
}
```

这里的含义是：

```plain
source.panel
```

贴袋裁片名称。

```plain
source.edge
```

贴袋上需要缝合的边。

```plain
target.panel
```

贴袋所附着的裤片或裙片。

```plain
target.segment
```

主体裁片内部的目标缝线，坐标必须是 **目标主体裁片的二维局部坐标**。

---

## 需要查看自己的贴袋 panel：
```python
pocket_panel.edges
```

确定：

```plain
哪条是袋口
哪条是袋底
哪两条是侧边
```

原则是：

```plain
袋底 + 两侧加入 surface_stitches
袋口不加入
```

---

# 三、最容易出错的是坐标系
你原来的实现里，贴袋顶点直接采用了目标前片的二维坐标，而且贴袋复制了目标衣片的 rotation，因此贴袋边端点可以直接作为目标主体上的 `segment`。

但学妹现在的部件化贴袋可能是这样创建的：

```python
pocket.vertices = [
    [0, 0],
    [width, 0],
    [width, height],
    [0, height]
]
```

然后通过：

```python
translation
rotation
```

摆到裤片前面。

这种情况下，**不能直接把贴袋的 **`**[0, 0]—[width, 0]**`** 当作裤片内部线段**。需要先把贴袋局部坐标转换到目标裤片局部坐标：

```python
def pocket_point_to_target_2d(
    point_2d,
    pocket_rotation,
    pocket_translation,
    target_rotation,
    target_translation,
):
    point_3d = np.array([point_2d[0], point_2d[1], 0.0])

    R_pocket = rotation_tools.euler_xyz_to_R(
        np.asarray(pocket_rotation, dtype=float)
    )
    R_target = rotation_tools.euler_xyz_to_R(
        np.asarray(target_rotation, dtype=float)
    )

    world_point = (
        R_pocket @ point_3d
        + np.asarray(pocket_translation, dtype=float)
    )

    target_local = (
        R_target.T
        @ (
            world_point
            - np.asarray(target_translation, dtype=float)
        )
    )

    return target_local[:2].tolist()
```

然后：

```python
start_target = pocket_point_to_target_2d(
    pocket_start,
    pocket_rotation,
    pocket_translation,
    target_rotation,
    target_translation,
)

end_target = pocket_point_to_target_2d(
    pocket_end,
    pocket_rotation,
    pocket_translation,
    target_rotation,
    target_translation,
)
```

再写入：

```python
"segment": [start_target, end_target]
```

这是能否正确缝到裤片对应位置的关键。

---

# 四、`BoxMesh` 中真正需要移植的函数
因为她已经知道：

+ 哪个是贴袋；
+ 贴在哪个下装 panel 上；
+ 哪些边需要缝；
+ 贴袋的位置。

真正需要复制到 `class BoxMesh` 中的是下面这些。

## 1. 局部顶点转全局顶点
```python
def _local_to_global_vertex_id(self, panel, loc_id):
    n_stitches_panel = panel.n_stitches

    if loc_id < n_stitches_panel:
        return self.verts_loc_glob[
            (panel.panel_name, loc_id)
        ]

    return (
        loc_id
        + panel.glob_offset
        - n_stitches_panel
    )
```

---

## 2. 沿主体内部目标线段找最近网格顶点
```python
def _surface_stitch_target_loc_ids(
    self,
    panel,
    segment,
    count,
    max_distance=None,
):
    if count <= 0:
        return []

    if max_distance is None:
        max_distance = max(
            self.mesh_resolution * 1.75,
            0.25,
        )

    vertices = np.asarray(
        panel.panel_vertices,
        dtype=float,
    )

    start = np.asarray(segment[0], dtype=float)
    end = np.asarray(segment[1], dtype=float)

    direction = end - start
    denominator = float(
        np.dot(direction, direction)
    )

    if denominator < 1e-8:
        raise PatternLoadingError(
            "Surface-stitch target segment "
            "is zero-length"
        )

    selected = []
    used = set()

    for t in np.linspace(0.0, 1.0, count):
        requested_point = start + direction * t

        distances = np.linalg.norm(
            vertices - requested_point,
            axis=1,
        )

        ordered_ids = np.argsort(distances)
        chosen = None

        for loc_id in ordered_ids:
            loc_id = int(loc_id)

            if loc_id in used:
                continue

            chosen = loc_id
            break

        if chosen is None:
            continue

        if distances[chosen] > max_distance:
            print(
                f"{self.__class__.__name__}::"
                f"{self.name}::WARNING::"
                f"surface stitch on "
                f"{panel.panel_name} is "
                f"{distances[chosen]:.3f} cm "
                f"from requested segment"
            )

        selected.append(chosen)
        used.add(chosen)

    return selected
```

它的作用不是修改 2D 样板，而是在三角网格生成后，沿目标线段寻找一排最接近的裤片网格顶点。

---

## 3. 建立贴袋边与主体内部顶点的对应关系
可以使用简化版，不做自动推断：

```python
def _surface_stitch_pairs(self):
    pairs = []

    surface_stitches = self.pattern.get(
        "surface_stitches",
        [],
    )

    if not surface_stitches:
        return pairs

    for stitch_id, stitch in enumerate(
        surface_stitches
    ):
        try:
            source = stitch["source"]
            target = stitch["target"]

            source_panel = self.panels[
                source["panel"]
            ]
            target_panel = self.panels[
                target["panel"]
            ]

            source_edge = source_panel.edges[
                int(source["edge"])
            ]

            target_segment = target["segment"]

        except (KeyError, TypeError, IndexError) as exc:
            raise PatternLoadingError(
                f"Invalid surface stitch "
                f"{stitch_id}: {stitch}"
            ) from exc

        source_loc_ids = list(
            source_edge.vertex_range
        )

        if stitch.get("swap", False):
            source_loc_ids.reverse()

        target_loc_ids = (
            self._surface_stitch_target_loc_ids(
                target_panel,
                target_segment,
                len(source_loc_ids),
            )
        )

        if len(target_loc_ids) != len(
            source_loc_ids
        ):
            raise PatternLoadingError(
                f"Surface stitch {stitch_id} "
                f"mapped "
                f"{len(target_loc_ids)} of "
                f"{len(source_loc_ids)} vertices"
            )

        for source_id, target_id in zip(
            source_loc_ids,
            target_loc_ids,
        ):
            pairs.append(
                (
                    self._local_to_global_vertex_id(
                        source_panel,
                        source_id,
                    ),
                    self._local_to_global_vertex_id(
                        target_panel,
                        target_id,
                    ),
                    f"stitch_surface_{stitch_id}",
                )
            )

    return pairs
```

你的原始实现也是先取得 `source_edge.vertex_range`，再沿目标 segment 找相同数量的主体网格顶点。

---

## 4. 合并对应顶点
还需要完整复制你提交中的：

```python
def _remap_surface_stitch_vertices(self, pairs):
    ...
```

以及：

```python
def _add_surface_stitch_orig_lens(self, old_to_new):
    ...
```

这里不要只写：

```python
self.vertices[source] = self.vertices[target]
```

因为仅修改坐标并不能真正缝合。必须重新映射：

```plain
vertices
faces
faces_with_texture
stitch_segmentation
orig_lens
```

你原来的实现用并查集将每组：

```plain
贴袋边顶点
+
裤片内部顶点
```

合并为同一个网格顶点，并删除顶点合并后产生的退化三角形。

---

## 5. 增加最终调用入口
```python
def apply_surface_stitches(self):
    pairs = self._surface_stitch_pairs()

    if not pairs:
        return

    vertex_count_before = len(self.vertices)

    self._remap_surface_stitch_vertices(
        pairs
    )

    print(
        f"{self.__class__.__name__}::"
        f"{self.name}::INFO::"
        f"Applied surface stitches; "
        f"merged "
        f"{vertex_count_before - len(self.vertices)} "
        f"mesh vertices"
    )
```

然后由前面修改过的 `BoxMesh.load()` 调用。

---

# 五、初始摆放也要处理，否则口袋会和裤片共面
在进入仿真前，贴袋不能和裤片完全处在同一个平面，否则容易：

+ 初始穿插；
+ 面重叠；
+ 法向不稳定；
+ Warp 一开始就出现自碰撞问题。

因此生成贴袋 panel 时，需要让贴袋沿目标裤片外法向稍微偏离，例如约：

```python
surface_offset_cm = 1.0
```

或者：

```python
surface_offset_cm = 1.5
```

你原来的实现是复制目标 panel 的 rotation 和 translation，再将贴袋向身体外侧偏移。

更稳妥的做法是沿目标 panel 的法向移动，而不是一律修改世界坐标 Z：

```python
R_target = rotation_tools.euler_xyz_to_R(
    np.asarray(target_rotation, dtype=float)
)

target_normal = R_target @ np.array(
    [0.0, 0.0, 1.0]
)

pocket_translation = (
    np.asarray(target_translation, dtype=float)
    + target_normal * surface_offset_cm
).tolist()
```

对于裙片、裤片发生旋转的情况，这比固定：

```python
translation[2] += 1.5
```

更可靠。

---

因为她已经通过下装部件模块完成：

```plain
GUI 参数选择
贴袋部件创建
2D 贴袋裁片显示
```

<font style="background-color:#FBDE28;">她只要补：</font>

```plain
surface_stitches 数据
+
BoxMesh 内部顶点映射与合并
```

---

## 总结逻辑
原 GarmentCode 的 `pattern.stitches` 只能缝两条裁片边界，不能表示“贴袋边缝到裤片内部”，因此需要在最终 specification 中新增 `pattern.surface_stitches`。每条记录保存贴袋 panel、贴袋 edge、目标裤片 panel，以及目标裤片局部二维坐标中的 segment。袋底和左右侧边各写一条，袋口不写。

然后修改 `pygarment/meshgen/boxmeshgen.py`：在 `BoxMesh.load()` 中搜索 `self.finalise_mesh()`，在它后面、`self.loaded = True` 前调用 `self.apply_surface_stitches()`。再把 surface stitch 的顶点匹配和顶点合并函数加入 `BoxMesh` 类。它会取得贴袋边上的网格顶点，沿目标裤片内部 segment 找最近的裤片网格顶点，再用并查集合并对应顶点，同时重新映射 faces、faces_with_texture、stitch_segmentation 和 orig_lens。

注意 target segment 必须是目标裤片的局部 2D 坐标；如果贴袋使用自己的局部坐标和独立 translation/rotation，需要先转换到目标裤片坐标系。另外，贴袋初始位置要沿目标裤片外法向偏移约 1–1.5 cm，避免与主体共面重叠。

