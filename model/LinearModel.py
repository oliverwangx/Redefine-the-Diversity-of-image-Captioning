import torch
import torch.nn as nn
import torch.nn.functional as F

class LinearModel(nn.Module):
    def __init__(self):
        super(LinearModel, self).__init__()
        self.conv1 = nn.Conv2d(512, 1024, (10, 9), (4, 6))
        self.conv2 = nn.Conv2d(512, 1024, 1, 1)

    def forward(self, image_pair):
        global_feat, box_feat = image_pair
        global_feat = global_feat.unsqueeze(dim=0)
        global_feat = self.conv1(global_feat)
        global_hidden = F.avg_pool2d(global_feat, 7).squeeze()
        box_feat = self.conv2(box_feat)
        return global_feat, global_hidden, box_feat


if __name__ == "__main__":
    global_feat = torch.randn(512, 34, 45)
    box_feat = torch.randn(75, 512, 7, 7)
    image_pair = [global_feat, box_feat]

    lm = LinearModel()
    gf, gh, bf = lm(image_pair)
    print(gf.shape, gh.shape, bf.shape)