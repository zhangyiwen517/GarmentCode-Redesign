from gui.callbacks import GUIState


def test_builds_absolute_static_3d_url():
    state = GUIState.__new__(GUIState)
    state.path_static_3d = '/geo'
    state.garm_3d_filename = 'garm_3d_test.glb'

    assert state._get_static_3d_url() == '/geo/garm_3d_test.glb'
