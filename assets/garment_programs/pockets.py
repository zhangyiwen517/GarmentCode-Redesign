import pygarment as pyg

class Pocket(pyg.Component):
    """口袋基类，所有口袋组件继承此类"""
    def __init__(self, name):
        super().__init__(name)


class PatchPocket(Pocket):
    """
    贴袋：直接缝合在裤片表面的口袋
    参数：
      - width: 袋口宽度 (cm)
      - depth: 袋身深度 (cm)
      - pocket_type: 口袋类型，可选 'rectangular' / 'rounded' / 'pointed'
      - bottom_radius: 袋底圆角半径 (cm)，默认 1.5
      - hem_width: 袋口折边宽度 (cm)，默认 1.5
    """
    def __init__(self, name, width, depth, pocket_type='rectangular', bottom_radius=None, hem_width=1.5):
        super().__init__(name)

        self.panel = pyg.Panel(f'{name}_panel')
        pocket_type = str(pocket_type or 'rectangular').lower()
        if pocket_type not in {'rectangular', 'rounded', 'pointed'}:
            pocket_type = 'rectangular'

        if pocket_type == 'rectangular':
            points = [
                [0, 0],
                [width, 0],
                [width, depth],
                [0, depth],
            ]
            edge_points = []
            for i in range(len(points)):
                p0 = points[i]
                p1 = points[(i + 1) % len(points)]
                edge_points.append((p0, p1))
            edges = [pyg.Edge(start, end) for start, end in edge_points]
            self.panel.edges = pyg.EdgeSequence(*edges).close_loop()
            
        elif pocket_type == 'rounded':
            if bottom_radius is None:
                bottom_radius = min(width, depth) / 2.0
            bottom_radius = min(bottom_radius, width / 2.0, depth / 2.0)
            top_left = [0, 0]
            top_right = [width, 0]
            bottom_right = [width, depth - bottom_radius]
            bottom_left = [0, depth - bottom_radius]
            top_edge = pyg.Edge(top_left, top_right)
            right_edge = pyg.Edge(top_right, bottom_right)
            left_edge = pyg.Edge(bottom_left, top_left)
            bottom_edge = pyg.CircleEdgeFactory.from_three_points(
                bottom_right,
                bottom_left,
                [width / 2.0, depth],
                relative=False
            )
            edges = [top_edge, right_edge, bottom_edge, left_edge]
            self.panel.edges = pyg.EdgeSequence(*edges).close_loop()
        else:  # pointed
            top_left = [0, 0]
            top_right = [width, 0]
            point_height = min(width * 0.45, depth * 0.35)
            vertical_height = max(depth - point_height, depth * 0.5)
            right_lower = [width, vertical_height]
            left_lower = [0, vertical_height]
            tip = [width * 0.5, depth]
            top_edge = pyg.Edge(top_left, top_right)
            right_edge = pyg.Edge(top_right, right_lower)
            lower_right_edge = pyg.Edge(right_lower, tip)
            lower_left_edge = pyg.Edge(tip, left_lower)
            left_edge = pyg.Edge(left_lower, top_left)
            edges = [top_edge, right_edge, lower_right_edge, lower_left_edge, left_edge]
            self.panel.edges = pyg.EdgeSequence(*edges).close_loop()

        # # 选择袋口与裤片缝合的边：仅使用袋口上边（与裤片口袋接口长度匹配）
        # top_edge = edges[0]
        # attach_edges = pyg.EdgeSequence(top_edge)
        # self.panel.interfaces['attach'] = pyg.Interface(self.panel, attach_edges)

        # self.interfaces = {
        #     'attach': self.panel.interfaces['attach']
        # } # jingma
        self.shape_type = pocket_type


class WeltPocket(Pocket):
    """
    挖袋（开袋）：袋口以嵌线形式开口，包含袋布和嵌线
    为简化，此处仅实现嵌线部分，袋布可另加
    """
    def __init__(self, name, width, welt_width=0.6, depth=12):
        super().__init__(name)
        # 挖袋较复杂，此处仅示意结构，实际需创建嵌线面板和袋布面板
        # 我们创建一个简单的嵌线面板（矩形）
        panel = pyg.Panel(f'{name}_welt')
        # 定义嵌线（窄条）
        welt_edges = pyg.EdgeSequence(
            pyg.Edge([0, 0], [width, 0]),
            pyg.Edge([width, 0], [width, welt_width]),
            pyg.Edge([width, welt_width], [0, welt_width]),
            pyg.Edge([0, welt_width], [0, 0])
        ).close_loop()
        panel.edges = welt_edges
        panel.interfaces['attach'] = pyg.Interface(panel, pyg.EdgeSequence([0,0], [width,0]))
        self.panels = [panel]
        self.interfaces = {'attach': panel.interfaces['attach']}
        # 袋布可额外添加，此处略


class SideSeamPocket(Pocket):
    """
    侧缝插袋：利用侧缝结构，袋口位于侧缝线上
    """
    def __init__(self, name, width, depth):
        super().__init__(name)
        # 插袋需要与侧缝匹配，此处仅创建袋布面板（示意）
        panel = pyg.Panel(f'{name}_bag')
        # 袋布为矩形，一侧与侧缝缝合
        panel.edges = pyg.EdgeSequence(
            pyg.Edge([0, 0], [width, 0]),
            pyg.Edge([width, 0], [width, depth]),
            pyg.Edge([width, depth], [0, depth]),
            pyg.Edge([0, depth], [0, 0])
        ).close_loop()
        panel.interfaces['attach'] = pyg.Interface(panel, pyg.EdgeSequence([0,0], [width,0]))
        self.panels = [panel]
        self.interfaces = {'attach': panel.interfaces['attach']}