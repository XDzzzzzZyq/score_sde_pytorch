import ml_collections
from configs.pinn.pinn_default_configs import get_default_configs


def get_config():
  config = get_default_configs()
  
  data = config.data
  data.dataset = 'PDE'
  data.dt = 1.75

  return config